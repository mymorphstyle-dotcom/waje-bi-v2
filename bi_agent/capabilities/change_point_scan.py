from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from math import fsum, hypot, isfinite, sqrt
from statistics import variance
from typing import Any

from bi_agent.capabilities import make_evidence_envelope


class ChangePointScanContractError(ValueError):
    pass


def change_point_scan(
    rows: Iterable[Mapping[str, Any]],
    *,
    time_key: str,
    value_key: str,
    min_total_samples: int,
    min_segment_samples: int,
    min_relative_level_shift: float,
    min_standardized_level_shift: float,
    max_candidates: int,
    result_refs: tuple[str, ...] = (),
    evidence_ref: str | None = None,
):
    """Detect candidate level shifts in one ordered aggregate metric series.

    Every eligible split is evaluated against two contract-owned thresholds:
    a symmetric relative mean shift and a Welch standard-error score. The
    output remains an anomaly candidate and never establishes a cause.
    """

    time_key = _required_name(time_key, "change_point_scan_time_key_invalid")
    value_key = _required_name(value_key, "change_point_scan_value_key_invalid")
    _validate_parameters(
        min_total_samples=min_total_samples,
        min_segment_samples=min_segment_samples,
        min_relative_level_shift=min_relative_level_shift,
        min_standardized_level_shift=min_standardized_level_shift,
        max_candidates=max_candidates,
    )
    prepared = _prepare_rows(rows, time_key=time_key, value_key=value_key)
    configuration = {
        "min_total_samples": min_total_samples,
        "min_segment_samples": min_segment_samples,
        "min_relative_level_shift": float(min_relative_level_shift),
        "min_standardized_level_shift": float(min_standardized_level_shift),
        "max_candidates": max_candidates,
    }
    if len(prepared) < min_total_samples:
        return make_evidence_envelope(
            "change_point_scan",
            evidence_ref=evidence_ref,
            evidence_type="insufficient_evidence",
            strength="insufficient",
            wording_limit="insufficient",
            numeric_facts={"sample_count": len(prepared), "candidate_count": 0},
            typed_payload={
                "time_key": time_key,
                "value_key": value_key,
                "sample_count": len(prepared),
                "candidate_count": 0,
                "candidates": (),
                "configuration": configuration,
                "claim_ceiling": "anomaly_candidate",
                "causal_claim_allowed": False,
            },
            limitations=("insufficient_ordered_samples",),
            result_refs=result_refs,
        )

    candidates = []
    values = tuple(item[2] for item in prepared)
    for split_index in range(
        min_segment_samples,
        len(prepared) - min_segment_samples + 1,
    ):
        left = values[:split_index]
        right = values[split_index:]
        left_mean = _sample_mean(left, "left_mean")
        right_mean = _sample_mean(right, "right_mean")
        delta = _finite_derived(
            right_mean - left_mean,
            "level_delta",
        )
        absolute_delta = _finite_derived(abs(delta), "absolute_level_delta")
        relative_denominator = _finite_derived(
            max(abs(left_mean), abs(right_mean)),
            "relative_level_denominator",
        )
        relative_shift = _finite_derived(
            (
                absolute_delta / relative_denominator
                if relative_denominator > 0
                else 0.0
            ),
            "relative_level_shift",
        )
        left_variance = _sample_variance(
            left,
            center=left_mean,
            statistic="left_variance",
        )
        right_variance = _sample_variance(
            right,
            center=right_mean,
            statistic="right_variance",
        )
        left_standard_error = _finite_derived(
            sqrt(
                _finite_derived(
                    left_variance / len(left),
                    "left_variance_of_mean",
                )
            ),
            "left_standard_error",
        )
        right_standard_error = _finite_derived(
            sqrt(
                _finite_derived(
                    right_variance / len(right),
                    "right_variance_of_mean",
                )
            ),
            "right_standard_error",
        )
        standard_error = _finite_derived(
            hypot(left_standard_error, right_standard_error),
            "standard_error",
        )
        zero_variance_shift = standard_error == 0.0 and delta != 0.0
        standardized_shift = (
            _finite_derived(
                absolute_delta / standard_error,
                "standardized_level_shift",
            )
            if standard_error > 0.0
            else None
        )
        standardized_threshold_met = zero_variance_shift or (
            standardized_shift is not None
            and standardized_shift >= min_standardized_level_shift
        )
        if relative_shift < min_relative_level_shift or not standardized_threshold_met:
            continue
        candidates.append(
            {
                "split_index": split_index,
                "left_end_time": _display_time(prepared[split_index - 1][0]),
                "right_start_time": _display_time(prepared[split_index][0]),
                "left_sample_count": len(left),
                "right_sample_count": len(right),
                "left_mean": left_mean,
                "right_mean": right_mean,
                "level_delta": delta,
                "relative_level_shift": relative_shift,
                "standardized_level_shift": standardized_shift,
                "zero_variance_shift": zero_variance_shift,
            }
        )

    ranked = tuple(
        sorted(
            candidates,
            key=lambda item: (
                item["zero_variance_shift"],
                item["standardized_level_shift"] or 0.0,
                item["relative_level_shift"],
                -item["split_index"],
            ),
            reverse=True,
        )[:max_candidates]
    )
    limitations = () if ranked else ("no_level_shift_met_contract_thresholds",)
    numeric_facts: dict[str, Any] = {
        "sample_count": len(prepared),
        "eligible_split_count": len(prepared) - 2 * min_segment_samples + 1,
        "candidate_count": len(ranked),
    }
    if ranked:
        numeric_facts.update(
            {
                "largest_relative_level_shift": ranked[0]["relative_level_shift"],
                "top_split_index": ranked[0]["split_index"],
            }
        )
    return make_evidence_envelope(
        "change_point_scan",
        evidence_ref=evidence_ref,
        evidence_type="statistical_association",
        strength="anomaly_candidate",
        wording_limit="anomaly_candidate",
        numeric_facts=numeric_facts,
        typed_payload={
            "time_key": time_key,
            "value_key": value_key,
            "sample_count": len(prepared),
            "candidate_count": len(ranked),
            "candidates": ranked,
            "configuration": configuration,
            "claim_ceiling": "anomaly_candidate",
            "causal_claim_allowed": False,
            "multiple_split_search": True,
        },
        limitations=limitations,
        result_refs=result_refs,
    )


def _prepare_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    time_key: str,
    value_key: str,
) -> tuple[tuple[Any, tuple[int, Any], float], ...]:
    prepared = []
    seen_times: set[tuple[str, tuple[int, Any]]] = set()
    time_family = ""
    for row in rows:
        if not isinstance(row, Mapping):
            raise ChangePointScanContractError("change_point_scan_row_invalid")
        if time_key not in row:
            raise ChangePointScanContractError("change_point_scan_time_missing")
        if value_key not in row:
            raise ChangePointScanContractError("change_point_scan_value_missing")
        time_value = row[time_key]
        family, sort_key = _time_sort_key(time_value)
        identity = (family, sort_key)
        if identity in seen_times:
            raise ChangePointScanContractError("change_point_scan_time_duplicate")
        seen_times.add(identity)
        if time_family and family != time_family:
            raise ChangePointScanContractError(
                "change_point_scan_time_type_inconsistent"
            )
        time_family = family
        prepared.append((time_value, sort_key, _finite_number(row[value_key])))
    return tuple(sorted(prepared, key=lambda item: item[1]))


def _time_sort_key(value: Any) -> tuple[str, tuple[int, Any]]:
    if isinstance(value, datetime):
        return "temporal", (0, value.isoformat())
    if isinstance(value, date):
        return "temporal", (0, value.isoformat())
    if isinstance(value, bool):
        raise ChangePointScanContractError("change_point_scan_time_invalid")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not isfinite(value):
            raise ChangePointScanContractError("change_point_scan_time_invalid")
        return "numeric", (1, Decimal(str(value)))
    if isinstance(value, str) and value and value == value.strip():
        return "text", (2, value)
    raise ChangePointScanContractError("change_point_scan_time_invalid")


def _display_time(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _sample_mean(values: tuple[float, ...], statistic: str) -> float:
    try:
        result = fsum(value / len(values) for value in values)
    except (OverflowError, ValueError) as exc:
        raise ChangePointScanContractError(
            f"change_point_scan_derived_statistic_nonfinite:{statistic}"
        ) from exc
    return _finite_derived(result, statistic)


def _sample_variance(
    values: tuple[float, ...],
    *,
    center: float,
    statistic: str,
) -> float:
    if len(values) < 2:
        return 0.0
    try:
        result = variance(values, xbar=center)
    except (OverflowError, ValueError) as exc:
        raise ChangePointScanContractError(
            f"change_point_scan_derived_statistic_nonfinite:{statistic}"
        ) from exc
    return _finite_derived(result, statistic)


def _finite_derived(value: Any, statistic: str) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ChangePointScanContractError(
            f"change_point_scan_derived_statistic_nonfinite:{statistic}"
        ) from exc
    if not isfinite(normalized):
        raise ChangePointScanContractError(
            f"change_point_scan_derived_statistic_nonfinite:{statistic}"
        )
    return normalized


def _finite_number(value: Any) -> float:
    if isinstance(value, bool):
        raise ChangePointScanContractError("change_point_scan_value_invalid")
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ChangePointScanContractError("change_point_scan_value_invalid") from exc
    if not isfinite(normalized):
        raise ChangePointScanContractError("change_point_scan_value_invalid")
    return normalized


def _required_name(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ChangePointScanContractError(error)
    return value


def _positive_number(value: Any, error: str) -> float:
    if isinstance(value, bool):
        raise ChangePointScanContractError(error)
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ChangePointScanContractError(error) from exc
    if not isfinite(normalized):
        raise ChangePointScanContractError(error)
    if normalized <= 0:
        raise ChangePointScanContractError(error)
    return normalized


def _validate_parameters(
    *,
    min_total_samples: int,
    min_segment_samples: int,
    min_relative_level_shift: float,
    min_standardized_level_shift: float,
    max_candidates: int,
) -> None:
    if type(min_segment_samples) is not int or min_segment_samples < 2:
        raise ChangePointScanContractError(
            "change_point_scan_min_segment_samples_invalid"
        )
    if (
        type(min_total_samples) is not int
        or min_total_samples < 2 * min_segment_samples
    ):
        raise ChangePointScanContractError(
            "change_point_scan_min_total_samples_invalid"
        )
    _positive_number(
        min_relative_level_shift,
        "change_point_scan_relative_threshold_invalid",
    )
    _positive_number(
        min_standardized_level_shift,
        "change_point_scan_standardized_threshold_invalid",
    )
    if type(max_candidates) is not int or max_candidates < 1:
        raise ChangePointScanContractError("change_point_scan_max_candidates_invalid")


__all__ = (
    "ChangePointScanContractError",
    "change_point_scan",
)
