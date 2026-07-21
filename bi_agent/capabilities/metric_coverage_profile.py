from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from math import isfinite
from typing import Any

from bi_agent.capabilities import make_evidence_envelope


class MetricCoverageProfileContractError(ValueError):
    pass


_COVERAGE_RECORD_FIELDS = frozenset(
    {
        "result_ref",
        "dataset_id",
        "snapshot_refs",
        "completeness_report_ref",
        "completeness_status",
        "analysis_readiness",
        "windows",
    }
)
_WINDOW_FIELDS = frozenset({"window_id", "required_days", "observed_days"})
_COMPLETENESS_STATES = frozenset(
    {"complete", "partial", "empty", "invalid", "truncated", "stale"}
)
_READINESS_STATES = frozenset({"ready", "degraded", "blocked"})


def metric_coverage_profile(
    rows: Iterable[Mapping[str, Any]],
    *,
    metric_id: str,
    value_key: str,
    result_ref_key: str,
    window_id_key: str,
    observation_key: str,
    source_row_count_key: str,
    coverage_records: Sequence[Mapping[str, Any]],
    result_refs: tuple[str, ...] = (),
    evidence_ref: str | None = None,
):
    """Describe metric coverage without promoting it into metric evidence."""

    metric_id = _required_name(metric_id, "metric_coverage_metric_id_invalid")
    value_key = _required_name(value_key, "metric_coverage_value_key_invalid")
    result_ref_key = _required_name(
        result_ref_key, "metric_coverage_result_ref_key_invalid"
    )
    window_id_key = _required_name(
        window_id_key, "metric_coverage_window_id_key_invalid"
    )
    observation_key = _required_name(
        observation_key, "metric_coverage_observation_key_invalid"
    )
    source_row_count_key = _required_name(
        source_row_count_key,
        "metric_coverage_source_row_count_key_invalid",
    )
    normalized_records = _coverage_records(coverage_records)
    normalized_rows = _rows(
        rows,
        value_key=value_key,
        result_ref_key=result_ref_key,
        window_id_key=window_id_key,
        observation_key=observation_key,
        source_row_count_key=source_row_count_key,
        record_refs=frozenset(normalized_records),
    )
    if not normalized_records and not normalized_rows:
        return make_evidence_envelope(
            "metric_coverage_profile",
            evidence_ref=evidence_ref,
            evidence_type="insufficient_evidence",
            strength="insufficient",
            wording_limit="insufficient",
            numeric_facts={"dataset_count": 0, "covered_dataset_count": 0},
            typed_payload={
                "metric_id": metric_id,
                "dataset_profiles": (),
                "claim_ceiling": "trust_boundary",
                "metric_claim_allowed": False,
            },
            limitations=("coverage_evidence_absent",),
            result_refs=result_refs,
        )

    rows_by_result: dict[str, list[Mapping[str, Any]]] = {
        result_ref: [] for result_ref in normalized_records
    }
    for row in normalized_rows:
        rows_by_result[row[result_ref_key]].append(row)

    profiles = []
    limitations = []
    for result_ref, record in sorted(
        normalized_records.items(),
        key=lambda item: (item[1]["dataset_id"], item[0]),
    ):
        result_rows = rows_by_result[result_ref]
        declared_windows = {window["window_id"]: window for window in record["windows"]}
        observed_row_windows = {str(row[window_id_key]) for row in result_rows}
        undeclared = observed_row_windows - set(declared_windows)
        if undeclared:
            raise MetricCoverageProfileContractError("metric_coverage_window_unbound")
        window_profiles = tuple(
            {
                "window_id": window_id,
                "required_days": window["required_days"],
                "observed_days": window["observed_days"],
                "coverage_ratio": (window["observed_days"] / window["required_days"]),
                "row_count": sum(
                    1 for row in result_rows if row[window_id_key] == window_id
                ),
            }
            for window_id, window in sorted(declared_windows.items())
        )
        if any(item["row_count"] != item["observed_days"] for item in window_profiles):
            raise MetricCoverageProfileContractError(
                "metric_coverage_window_row_count_mismatch"
            )
        metric_non_null_count = sum(
            1 for row in result_rows if row[value_key] is not None
        )
        source_row_count = sum(int(row[source_row_count_key]) for row in result_rows)
        windows_covered = all(
            item["observed_days"] >= item["required_days"] for item in window_profiles
        )
        covered = (
            record["completeness_status"] == "complete"
            and record["analysis_readiness"] == "ready"
            and bool(result_rows)
            and metric_non_null_count == len(result_rows)
            and source_row_count > 0
            and windows_covered
        )
        coverage_state = "covered" if covered else "limited"
        if not covered:
            limitations.append(f"coverage_limited:{record['dataset_id']}")
        profiles.append(
            {
                "dataset_id": record["dataset_id"],
                "result_ref": result_ref,
                "snapshot_refs": record["snapshot_refs"],
                "completeness_report_ref": record["completeness_report_ref"],
                "completeness_status": record["completeness_status"],
                "analysis_readiness": record["analysis_readiness"],
                "coverage_state": coverage_state,
                "row_count": len(result_rows),
                "source_row_count": source_row_count,
                "metric_non_null_count": metric_non_null_count,
                "metric_non_null_ratio": (
                    metric_non_null_count / len(result_rows) if result_rows else 0.0
                ),
                "windows": window_profiles,
            }
        )

    covered_count = sum(
        1 for profile in profiles if profile["coverage_state"] == "covered"
    )
    return make_evidence_envelope(
        "metric_coverage_profile",
        evidence_ref=evidence_ref,
        evidence_type="trust_boundary",
        strength="trust_boundary",
        wording_limit="trust_boundary",
        numeric_facts={
            "dataset_count": len(profiles),
            "covered_dataset_count": covered_count,
            "limited_dataset_count": len(profiles) - covered_count,
        },
        typed_payload={
            "metric_id": metric_id,
            "dataset_profiles": tuple(profiles),
            "claim_ceiling": "trust_boundary",
            "metric_claim_allowed": False,
        },
        limitations=tuple(limitations),
        result_refs=result_refs,
    )


def _coverage_records(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise MetricCoverageProfileContractError("metric_coverage_records_invalid")
    by_ref: dict[str, dict[str, Any]] = {}
    dataset_ids: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping) or set(record) != _COVERAGE_RECORD_FIELDS:
            raise MetricCoverageProfileContractError(
                "metric_coverage_record_shape_invalid"
            )
        result_ref = _required_name(
            record["result_ref"], "metric_coverage_record_result_ref_invalid"
        )
        dataset_id = _required_name(
            record["dataset_id"], "metric_coverage_record_dataset_id_invalid"
        )
        if result_ref in by_ref:
            raise MetricCoverageProfileContractError(
                "metric_coverage_record_result_ref_duplicate"
            )
        if dataset_id in dataset_ids:
            raise MetricCoverageProfileContractError(
                "metric_coverage_record_dataset_id_duplicate"
            )
        snapshot_refs = _string_tuple(
            record["snapshot_refs"],
            "metric_coverage_snapshot_refs_invalid",
            allow_empty=False,
        )
        completeness_report_ref = _required_name(
            record["completeness_report_ref"],
            "metric_coverage_completeness_report_ref_invalid",
        )
        completeness_status = record["completeness_status"]
        if completeness_status not in _COMPLETENESS_STATES:
            raise MetricCoverageProfileContractError(
                "metric_coverage_completeness_status_invalid"
            )
        analysis_readiness = record["analysis_readiness"]
        if analysis_readiness not in _READINESS_STATES:
            raise MetricCoverageProfileContractError(
                "metric_coverage_analysis_readiness_invalid"
            )
        windows = _windows(record["windows"])
        by_ref[result_ref] = {
            "result_ref": result_ref,
            "dataset_id": dataset_id,
            "snapshot_refs": snapshot_refs,
            "completeness_report_ref": completeness_report_ref,
            "completeness_status": completeness_status,
            "analysis_readiness": analysis_readiness,
            "windows": windows,
        }
        dataset_ids.add(dataset_id)
    return by_ref


def _windows(value: Any) -> tuple[dict[str, Any], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise MetricCoverageProfileContractError("metric_coverage_windows_invalid")
    normalized = []
    seen: set[str] = set()
    for window in value:
        if not isinstance(window, Mapping) or set(window) != _WINDOW_FIELDS:
            raise MetricCoverageProfileContractError(
                "metric_coverage_window_shape_invalid"
            )
        window_id = _required_name(
            window["window_id"], "metric_coverage_window_id_invalid"
        )
        if window_id in seen:
            raise MetricCoverageProfileContractError(
                "metric_coverage_window_id_duplicate"
            )
        required_days = window["required_days"]
        observed_days = window["observed_days"]
        if type(required_days) is not int or required_days < 1:
            raise MetricCoverageProfileContractError(
                "metric_coverage_required_days_invalid"
            )
        if type(observed_days) is not int or observed_days < 0:
            raise MetricCoverageProfileContractError(
                "metric_coverage_observed_days_invalid"
            )
        normalized.append(
            {
                "window_id": window_id,
                "required_days": required_days,
                "observed_days": observed_days,
            }
        )
        seen.add(window_id)
    return tuple(sorted(normalized, key=lambda item: item["window_id"]))


def _rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    value_key: str,
    result_ref_key: str,
    window_id_key: str,
    observation_key: str,
    source_row_count_key: str,
    record_refs: frozenset[str],
) -> tuple[Mapping[str, Any], ...]:
    normalized = []
    seen: set[tuple[str, str, tuple[str, str]]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise MetricCoverageProfileContractError("metric_coverage_row_invalid")
        required = (
            result_ref_key,
            window_id_key,
            observation_key,
            value_key,
            source_row_count_key,
        )
        if any(key not in row for key in required):
            raise MetricCoverageProfileContractError(
                "metric_coverage_row_field_missing"
            )
        result_ref = _required_name(
            row[result_ref_key], "metric_coverage_row_result_ref_invalid"
        )
        if result_ref not in record_refs:
            raise MetricCoverageProfileContractError(
                "metric_coverage_result_ref_unbound"
            )
        window_id = _required_name(
            row[window_id_key], "metric_coverage_row_window_id_invalid"
        )
        observation = _observation_identity(row[observation_key])
        identity = (result_ref, window_id, observation)
        if identity in seen:
            raise MetricCoverageProfileContractError(
                "metric_coverage_observation_duplicate"
            )
        seen.add(identity)
        value = row[value_key]
        if value is not None:
            _finite_number(value, "metric_coverage_metric_value_invalid")
        source_row_count = row[source_row_count_key]
        if type(source_row_count) is not int or source_row_count < 0:
            raise MetricCoverageProfileContractError(
                "metric_coverage_source_row_count_invalid"
            )
        normalized.append(row)
    return tuple(normalized)


def _observation_identity(value: Any) -> tuple[str, str]:
    if value is None or isinstance(value, bool):
        raise MetricCoverageProfileContractError("metric_coverage_observation_invalid")
    if isinstance(value, str) and (not value or value != value.strip()):
        raise MetricCoverageProfileContractError("metric_coverage_observation_invalid")
    return type(value).__name__, repr(value)


def _required_name(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MetricCoverageProfileContractError(error)
    return value


def _string_tuple(value: Any, error: str, *, allow_empty: bool) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MetricCoverageProfileContractError(error)
    normalized = tuple(_required_name(item, error) for item in value)
    if (not allow_empty and not normalized) or len(normalized) != len(set(normalized)):
        raise MetricCoverageProfileContractError(error)
    return normalized


def _finite_number(value: Any, error: str) -> float:
    if isinstance(value, bool):
        raise MetricCoverageProfileContractError(error)
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MetricCoverageProfileContractError(error) from exc
    if not isfinite(normalized):
        raise MetricCoverageProfileContractError(error)
    return normalized


__all__ = (
    "MetricCoverageProfileContractError",
    "metric_coverage_profile",
)
