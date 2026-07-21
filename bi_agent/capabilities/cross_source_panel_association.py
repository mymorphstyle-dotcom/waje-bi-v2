from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable, Iterable, Mapping
from datetime import date, datetime
from math import copysign, isfinite, log, log1p, sqrt
from typing import Any

from bi_agent.capabilities import make_evidence_envelope


_MAPPING_AUTHORITY_STATUSES = frozenset(
    {
        "approved",
        "authoritative",
        "contracted",
        "reviewed",
        "verified",
    }
)
_TRANSFORM_ALIASES = {
    "level": "level",
    "difference": "difference",
    "daily_change": "difference",
    "absolute_change": "difference",
    "log_difference": "log_difference",
    "log_change": "log_difference",
    "log_return": "log_difference",
    "signed_log_difference": "signed_log_difference",
    "signed_log_change": "signed_log_difference",
}
_PAIR_COVERAGE_BASIS = "complete_transformed_lagged_pairs_over_aligned_opportunities"


def cross_source_panel_association(
    rows: Iterable[Mapping[str, Any]],
    *,
    time_key: str,
    panel_key: str,
    hypothesis: Mapping[str, Any],
    mapping_authority_status: str = "candidate_mechanical_crosswalk",
    mapping_coverage: float | None = None,
    mapping_coverage_basis: Mapping[str, Any] | None = None,
    min_samples: int = 30,
    min_panels: int = 3,
    min_panel_samples: int = 5,
    min_pair_coverage: float = 0.80,
    min_mapping_coverage: float = 0.80,
    min_direction_stability: float = 0.60,
    residual_tolerance: float = 1e-10,
    max_iterations: int = 1_000,
    result_refs: tuple[str, ...] = (),
    evidence_ref: str | None = None,
):
    """Test one explicit cross-source hypothesis in an aligned panel.

    Each input row must represent one unique ``time_key`` x ``panel_key`` cell.
    The hypothesis fixes the outcome, candidate, transform, and lag discovered by
    an upstream association screen. Transform and lag alignment are performed
    independently inside each panel before common time shocks and persistent
    panel-level differences are removed. The capability cannot publish causal
    effects, contribution amounts, or conclusions about any named panel.
    """

    normalized_hypothesis = _normalize_hypothesis(hypothesis)
    outcome_key = normalized_hypothesis["outcome_key"]
    candidate_key = normalized_hypothesis["candidate_key"]
    transform = normalized_hypothesis["transform"]
    lag = normalized_hypothesis["lag"]
    coverage_basis = _normalize_mapping_coverage_basis(mapping_coverage_basis)
    _validate_configuration(
        time_key=time_key,
        panel_key=panel_key,
        outcome_key=outcome_key,
        candidate_key=candidate_key,
        mapping_coverage=mapping_coverage,
        min_samples=min_samples,
        min_panels=min_panels,
        min_panel_samples=min_panel_samples,
        min_pair_coverage=min_pair_coverage,
        min_mapping_coverage=min_mapping_coverage,
        min_direction_stability=min_direction_stability,
        residual_tolerance=residual_tolerance,
        max_iterations=max_iterations,
    )
    prepared = _prepare_rows(
        rows,
        time_key=time_key,
        panel_key=panel_key,
        outcome_key=outcome_key,
        candidate_key=candidate_key,
    )
    total_rows = len(prepared)
    transformed, lag_aligned_opportunity_count = _apply_hypothesis_within_panels(
        prepared,
        transform=transform,
        lag=lag,
    )
    complete = tuple(row for row in transformed if row["complete"])
    complete_pair_count = len(complete)
    pair_coverage = (
        complete_pair_count / lag_aligned_opportunity_count
        if lag_aligned_opportunity_count
        else 0.0
    )

    complete_counts_by_panel: dict[Hashable, int] = defaultdict(int)
    for row in complete:
        complete_counts_by_panel[row["panel_token"]] += 1
    eligible_panel_tokens = {
        token
        for token, count in complete_counts_by_panel.items()
        if count >= min_panel_samples
    }
    analysis_rows = tuple(
        row for row in complete if row["panel_token"] in eligible_panel_tokens
    )
    analysis_sample_size = len(analysis_rows)
    analysis_panel_count = len(eligible_panel_tokens)
    analysis_time_count = len({row["time_token"] for row in analysis_rows})

    panel_tokens = tuple(row["panel_token"] for row in analysis_rows)
    time_tokens = tuple(row["time_token"] for row in analysis_rows)
    outcome_values = tuple(float(row["outcome"]) for row in analysis_rows)
    candidate_values = tuple(float(row["candidate"]) for row in analysis_rows)
    outcome_residuals, outcome_fe = _two_way_fixed_effect_residuals(
        outcome_values,
        panel_tokens=panel_tokens,
        time_tokens=time_tokens,
        tolerance=residual_tolerance,
        max_iterations=max_iterations,
    )
    candidate_residuals, candidate_fe = _two_way_fixed_effect_residuals(
        candidate_values,
        panel_tokens=panel_tokens,
        time_tokens=time_tokens,
        tolerance=residual_tolerance,
        max_iterations=max_iterations,
    )

    residual_variance_present = _has_variance(
        outcome_residuals, tolerance=residual_tolerance
    ) and _has_variance(candidate_residuals, tolerance=residual_tolerance)
    if residual_variance_present:
        residual_pearson = _pearson(
            outcome_residuals,
            candidate_residuals,
            tolerance=residual_tolerance,
        )
        residual_spearman = _spearman(
            outcome_residuals,
            candidate_residuals,
            tolerance=residual_tolerance,
        )
    else:
        residual_pearson = None
        residual_spearman = None

    direction = _within_panel_direction_stability(
        outcome_residuals,
        candidate_residuals,
        panel_tokens=panel_tokens,
        overall_coefficient=residual_pearson,
        min_panel_samples=min_panel_samples,
        min_direction_stability=min_direction_stability,
        min_panels=min_panels,
        tolerance=residual_tolerance,
    )

    samples_met = analysis_sample_size >= min_samples
    panels_met = analysis_panel_count >= min_panels
    fe_converged = outcome_fe["converged"] and candidate_fe["converged"]
    coefficient_available = (
        residual_pearson is not None and residual_spearman is not None
    )
    analysis_sufficient = (
        samples_met and panels_met and fe_converged and coefficient_available
    )

    normalized_mapping_authority = _normalize_mapping_authority_status(
        mapping_authority_status
    )
    mapping_authority_established = (
        normalized_mapping_authority in _MAPPING_AUTHORITY_STATUSES
    )
    mapping_coverage_sufficient = (
        mapping_coverage is not None and mapping_coverage >= min_mapping_coverage
    )
    pair_coverage_sufficient = pair_coverage >= min_pair_coverage
    stable_across_panels = bool(direction["stable"])
    publishable_association = (
        analysis_sufficient
        and mapping_authority_established
        and mapping_coverage_sufficient
        and pair_coverage_sufficient
        and stable_across_panels
    )

    evidence_type = (
        "statistical_association" if analysis_sufficient else "insufficient_evidence"
    )
    strength = "medium" if publishable_association else "low"
    wording_limit = (
        "statistical_association" if publishable_association else "sensitivity_only"
    )
    claim_ceiling = wording_limit
    limitations = _limitations(
        samples_met=samples_met,
        panels_met=panels_met,
        fe_converged=fe_converged,
        residual_variance_present=residual_variance_present,
        mapping_authority_established=mapping_authority_established,
        mapping_coverage=mapping_coverage,
        mapping_coverage_sufficient=mapping_coverage_sufficient,
        pair_coverage_sufficient=pair_coverage_sufficient,
        stable_across_panels=stable_across_panels,
    )

    numeric_facts = {
        "input_row_count": total_rows,
        "lag_aligned_opportunity_count": lag_aligned_opportunity_count,
        "complete_pair_count": complete_pair_count,
        "analysis_sample_size": analysis_sample_size,
        "input_panel_count": len({row["panel_token"] for row in prepared}),
        "complete_panel_count": len(complete_counts_by_panel),
        "analysis_panel_count": analysis_panel_count,
        "analysis_time_count": analysis_time_count,
        "pair_coverage": pair_coverage,
        "mapping_coverage": mapping_coverage,
        "residual_pearson": residual_pearson,
        "residual_spearman": residual_spearman,
        "directional_panel_count": direction["directional_panel_count"],
        "same_direction_panel_count": direction["same_direction_panel_count"],
        "same_direction_ratio": direction["same_direction_ratio"],
    }
    return make_evidence_envelope(
        "cross_source_panel_association",
        evidence_ref=evidence_ref,
        evidence_type=evidence_type,
        strength=strength,
        wording_limit=wording_limit,
        numeric_facts=numeric_facts,
        typed_payload={
            "analysis_role": "cross_source_panel_association",
            "time_key": time_key,
            "panel_key": panel_key,
            "hypothesis": normalized_hypothesis,
            "transformation": {
                "transform": transform,
                "scope": "within_panel",
            },
            "lag_alignment": {
                "lag": lag,
                "scope": "within_panel",
                "semantics": (
                    "positive_lag_means_candidate_precedes_outcome_by_"
                    "within_panel_aligned_observations"
                ),
            },
            "coverage": {
                "input_panel_time_cells": total_rows,
                "lag_aligned_opportunities": lag_aligned_opportunity_count,
                "complete_pairs": complete_pair_count,
                "pair_coverage": pair_coverage,
                "pair_coverage_basis": _PAIR_COVERAGE_BASIS,
                "minimum_pair_coverage": min_pair_coverage,
                "pair_coverage_sufficient": pair_coverage_sufficient,
                "eligible_analysis_rows": analysis_sample_size,
                "eligible_panel_count": analysis_panel_count,
            },
            "mapping": {
                "authority_status": normalized_mapping_authority,
                "authority_established": mapping_authority_established,
                "coverage": mapping_coverage,
                "coverage_basis": coverage_basis,
                "minimum_coverage": min_mapping_coverage,
                "coverage_known": mapping_coverage is not None,
                "coverage_sufficient": mapping_coverage_sufficient,
            },
            "minimum_requirements": {
                "minimum_samples": min_samples,
                "minimum_panels": min_panels,
                "minimum_samples_per_panel": min_panel_samples,
                "samples_met": samples_met,
                "panels_met": panels_met,
            },
            "two_way_fixed_effects": {
                "method": "alternating_projection_demeaning",
                "removed_effects": ("time_common_shock", "panel_fixed_difference"),
                "outcome_iterations": outcome_fe["iterations"],
                "candidate_iterations": candidate_fe["iterations"],
                "converged": fe_converged,
                "tolerance": residual_tolerance,
            },
            "aggregate_association": {
                "residual_pearson": residual_pearson,
                "residual_spearman": residual_spearman,
                "residual_variance_present": residual_variance_present,
            },
            "within_panel_direction_stability": direction,
            "claim_ceiling": claim_ceiling,
            "uncertainty_estimate_available": False,
            "causal_claim_allowed": False,
            "contribution_claim_allowed": False,
            "specific_panel_claim_allowed": False,
            "coefficient_is_contribution": False,
        },
        limitations=limitations,
        result_refs=result_refs,
    )


def _normalize_hypothesis(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("hypothesis must be a mapping")
    required = {"outcome_key", "candidate_key", "transform", "lag"}
    allowed = {*required, "hypothesis_id"}
    if set(value) - allowed:
        raise ValueError("hypothesis contains unsupported fields")
    if not required.issubset(value):
        raise ValueError(
            "hypothesis must declare outcome, candidate, transform, and lag"
        )
    outcome_key = str(value.get("outcome_key") or "").strip()
    candidate_key = str(value.get("candidate_key") or "").strip()
    if not outcome_key or not candidate_key or outcome_key == candidate_key:
        raise ValueError("hypothesis outcome and candidate must be distinct metrics")
    transform_token = str(value.get("transform") or "").strip().lower()
    try:
        transform = _TRANSFORM_ALIASES[transform_token]
    except KeyError as exc:
        raise ValueError(f"unsupported panel transform: {transform_token}") from exc
    lag = value.get("lag")
    if isinstance(lag, bool) or not isinstance(lag, int):
        raise ValueError("hypothesis lag must be an integer observation offset")
    hypothesis_id = str(value.get("hypothesis_id") or "").strip()
    if not hypothesis_id:
        hypothesis_id = f"{outcome_key}:{candidate_key}:{transform}:lag{lag}"
    return {
        "hypothesis_id": hypothesis_id,
        "outcome_key": outcome_key,
        "candidate_key": candidate_key,
        "transform": transform,
        "lag": lag,
    }


def _normalize_mapping_coverage_basis(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if value is None:
        return {"combination": "unknown"}
    if not isinstance(value, Mapping):
        raise TypeError("mapping_coverage_basis must be a mapping")
    normalized = dict(value)
    for side in ("outcome", "candidate"):
        detail = normalized.get(side)
        if detail is None:
            continue
        if not isinstance(detail, Mapping):
            raise TypeError(f"mapping_coverage_basis.{side} must be a mapping")
        coverage = detail.get("coverage")
        if coverage is not None:
            _validate_ratio(f"mapping_coverage_basis.{side}.coverage", coverage)
        normalized[side] = dict(detail)
    return normalized


def _validate_configuration(
    *,
    time_key: str,
    panel_key: str,
    outcome_key: str,
    candidate_key: str,
    mapping_coverage: float | None,
    min_samples: int,
    min_panels: int,
    min_panel_samples: int,
    min_pair_coverage: float,
    min_mapping_coverage: float,
    min_direction_stability: float,
    residual_tolerance: float,
    max_iterations: int,
) -> None:
    keys = (time_key, panel_key, outcome_key, candidate_key)
    if any(not isinstance(key, str) or not key.strip() for key in keys):
        raise ValueError("time, panel, outcome, and candidate keys must be non-empty")
    if len(set(keys)) != len(keys):
        raise ValueError("time, panel, outcome, and candidate keys must be distinct")
    if mapping_coverage is not None:
        _validate_ratio("mapping_coverage", mapping_coverage)
    _validate_ratio("min_pair_coverage", min_pair_coverage)
    _validate_ratio("min_mapping_coverage", min_mapping_coverage)
    _validate_ratio("min_direction_stability", min_direction_stability)
    if min_samples < 3:
        raise ValueError("min_samples must be at least 3")
    if min_panels < 2:
        raise ValueError("min_panels must be at least 2")
    if min_panel_samples < 3:
        raise ValueError("min_panel_samples must be at least 3")
    if residual_tolerance <= 0 or not isfinite(float(residual_tolerance)):
        raise ValueError("residual_tolerance must be a finite positive number")
    if max_iterations < 1:
        raise ValueError("max_iterations must be positive")


def _validate_ratio(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a numeric ratio between 0 and 1")
    if not isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")


def _prepare_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    time_key: str,
    panel_key: str,
    outcome_key: str,
    candidate_key: str,
) -> tuple[dict[str, Any], ...]:
    prepared: list[dict[str, Any]] = []
    seen_cells: set[tuple[Hashable, Hashable]] = set()
    required_keys = (time_key, panel_key, outcome_key, candidate_key)
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise TypeError("rows must contain mappings")
        missing = [key for key in required_keys if key not in row]
        if missing:
            raise ValueError(f"row {index} is missing required columns: {missing}")
        time_token = _group_token(row[time_key], key_name=time_key)
        panel_token = _group_token(row[panel_key], key_name=panel_key)
        cell = (time_token, panel_token)
        if cell in seen_cells:
            raise ValueError("panel-time cells must be unique before analysis")
        seen_cells.add(cell)
        outcome = _number(row[outcome_key])
        candidate = _number(row[candidate_key])
        prepared.append(
            {
                "time_value": row[time_key],
                "time_token": time_token,
                "panel_token": panel_token,
                "outcome": outcome,
                "candidate": candidate,
            }
        )
    return tuple(prepared)


def _apply_hypothesis_within_panels(
    rows: tuple[dict[str, Any], ...],
    *,
    transform: str,
    lag: int,
) -> tuple[tuple[dict[str, Any], ...], int]:
    rows_by_panel: dict[Hashable, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_panel[row["panel_token"]].append(row)

    projected: list[dict[str, Any]] = []
    aligned_opportunities = 0
    for panel_rows in rows_by_panel.values():
        ordered = sorted(
            panel_rows,
            key=lambda row: _time_sort_key(row["time_value"]),
        )
        outcome_values = _transform_series(
            tuple(row["outcome"] for row in ordered), transform
        )
        candidate_values = _transform_series(
            tuple(row["candidate"] for row in ordered), transform
        )
        for outcome_index, row in enumerate(ordered):
            candidate_index = outcome_index - lag
            if not 0 <= candidate_index < len(ordered):
                continue
            aligned_opportunities += 1
            outcome_value = outcome_values[outcome_index]
            candidate_value = candidate_values[candidate_index]
            projected.append(
                {
                    "time_token": row["time_token"],
                    "panel_token": row["panel_token"],
                    "outcome": outcome_value,
                    "candidate": candidate_value,
                    "complete": (
                        outcome_value is not None and candidate_value is not None
                    ),
                }
            )
    return tuple(projected), aligned_opportunities


def _transform_series(
    values: tuple[float | None, ...],
    transform: str,
) -> tuple[float | None, ...]:
    if transform == "level":
        return values
    transformed: list[float | None] = [None]
    for previous, current in zip(values, values[1:]):
        if previous is None or current is None:
            transformed.append(None)
        elif transform == "difference":
            transformed.append(current - previous)
        elif transform == "signed_log_difference":
            transformed.append(_signed_log(current) - _signed_log(previous))
        elif previous > 0.0 and current > 0.0:
            transformed.append(log(current) - log(previous))
        else:
            transformed.append(None)
    return tuple(transformed)


def _signed_log(value: float) -> float:
    if value == 0.0:
        return 0.0
    return copysign(log1p(abs(value)), value)


def _time_sort_key(value: Any) -> tuple[int, Any]:
    if isinstance(value, (datetime, date)):
        return 0, value.isoformat()
    if isinstance(value, bool):
        return 2, str(value)
    if isinstance(value, (int, float)) and isfinite(float(value)):
        return 1, float(value)
    return 2, str(value)


def _group_token(value: Any, *, key_name: str) -> Hashable:
    if value is None:
        raise ValueError(f"{key_name} cannot be null")
    try:
        hash(value)
    except TypeError as exc:
        raise TypeError(f"{key_name} values must be hashable") from exc
    return (type(value).__module__, type(value).__qualname__, value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _two_way_fixed_effect_residuals(
    values: tuple[float, ...],
    *,
    panel_tokens: tuple[Hashable, ...],
    time_tokens: tuple[Hashable, ...],
    tolerance: float,
    max_iterations: int,
) -> tuple[tuple[float, ...], dict[str, Any]]:
    if not values:
        return (), {"converged": False, "iterations": 0}
    residuals = list(values)
    converged = False
    iterations = 0
    for iterations in range(1, max_iterations + 1):
        residuals = _demean(residuals, panel_tokens)
        residuals = _demean(residuals, time_tokens)
        imbalance = max(
            _maximum_absolute_group_mean(residuals, panel_tokens),
            _maximum_absolute_group_mean(residuals, time_tokens),
        )
        if imbalance <= tolerance:
            converged = True
            break
    return tuple(residuals), {"converged": converged, "iterations": iterations}


def _demean(values: list[float], groups: tuple[Hashable, ...]) -> list[float]:
    sums: dict[Hashable, float] = defaultdict(float)
    counts: dict[Hashable, int] = defaultdict(int)
    for value, group in zip(values, groups):
        sums[group] += value
        counts[group] += 1
    means = {group: sums[group] / counts[group] for group in sums}
    return [value - means[group] for value, group in zip(values, groups)]


def _maximum_absolute_group_mean(
    values: list[float], groups: tuple[Hashable, ...]
) -> float:
    if not values:
        return 0.0
    sums: dict[Hashable, float] = defaultdict(float)
    counts: dict[Hashable, int] = defaultdict(int)
    for value, group in zip(values, groups):
        sums[group] += value
        counts[group] += 1
    return max(abs(sums[group] / counts[group]) for group in sums)


def _has_variance(values: tuple[float, ...], *, tolerance: float) -> bool:
    if len(values) < 2:
        return False
    mean_value = sum(values) / len(values)
    return sum((value - mean_value) ** 2 for value in values) > tolerance


def _pearson(
    left: tuple[float, ...], right: tuple[float, ...], *, tolerance: float
) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    left_centered = tuple(value - left_mean for value in left)
    right_centered = tuple(value - right_mean for value in right)
    left_sum_squares = sum(value * value for value in left_centered)
    right_sum_squares = sum(value * value for value in right_centered)
    if left_sum_squares <= tolerance or right_sum_squares <= tolerance:
        return None
    numerator = sum(
        left_value * right_value
        for left_value, right_value in zip(left_centered, right_centered)
    )
    coefficient = numerator / sqrt(left_sum_squares * right_sum_squares)
    return max(-1.0, min(1.0, coefficient))


def _spearman(
    left: tuple[float, ...], right: tuple[float, ...], *, tolerance: float
) -> float | None:
    return _pearson(
        _average_ranks(left),
        _average_ranks(right),
        tolerance=tolerance,
    )


def _average_ranks(values: tuple[float, ...]) -> tuple[float, ...]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        while end < len(ordered) and ordered[end][1] == ordered[cursor][1]:
            end += 1
        average_rank = (cursor + 1 + end) / 2.0
        for position in range(cursor, end):
            ranks[ordered[position][0]] = average_rank
        cursor = end
    return tuple(ranks)


def _within_panel_direction_stability(
    target_residuals: tuple[float, ...],
    candidate_residuals: tuple[float, ...],
    *,
    panel_tokens: tuple[Hashable, ...],
    overall_coefficient: float | None,
    min_panel_samples: int,
    min_direction_stability: float,
    min_panels: int,
    tolerance: float,
) -> dict[str, Any]:
    indexes_by_panel: dict[Hashable, list[int]] = defaultdict(list)
    for index, panel_token in enumerate(panel_tokens):
        indexes_by_panel[panel_token].append(index)

    positive_count = 0
    negative_count = 0
    zero_or_unavailable_count = 0
    for indexes in indexes_by_panel.values():
        if len(indexes) < min_panel_samples:
            zero_or_unavailable_count += 1
            continue
        coefficient = _pearson(
            tuple(target_residuals[index] for index in indexes),
            tuple(candidate_residuals[index] for index in indexes),
            tolerance=tolerance,
        )
        if coefficient is None or abs(coefficient) <= tolerance:
            zero_or_unavailable_count += 1
        elif coefficient > 0:
            positive_count += 1
        else:
            negative_count += 1

    directional_panel_count = positive_count + negative_count
    if overall_coefficient is None or abs(overall_coefficient) <= tolerance:
        same_direction_count = 0
        same_direction_ratio = None
    else:
        same_direction_count = (
            positive_count if overall_coefficient > 0 else negative_count
        )
        same_direction_ratio = (
            same_direction_count / directional_panel_count
            if directional_panel_count
            else None
        )
    stable = (
        directional_panel_count >= min_panels
        and same_direction_ratio is not None
        and same_direction_ratio >= min_direction_stability
    )
    return {
        "eligible_panel_count": len(indexes_by_panel),
        "directional_panel_count": directional_panel_count,
        "same_direction_panel_count": same_direction_count,
        "same_direction_ratio": same_direction_ratio,
        "positive_direction_panel_count": positive_count,
        "negative_direction_panel_count": negative_count,
        "zero_or_unavailable_panel_count": zero_or_unavailable_count,
        "minimum_same_direction_ratio": min_direction_stability,
        "stable": stable,
        "panel_identifiers_included": False,
    }


def _normalize_mapping_authority_status(mapping_authority_status: str) -> str:
    if not isinstance(mapping_authority_status, str):
        return "candidate_mechanical_crosswalk"
    normalized = mapping_authority_status.strip().lower().replace("-", "_")
    return normalized or "candidate_mechanical_crosswalk"


def _limitations(
    *,
    samples_met: bool,
    panels_met: bool,
    fe_converged: bool,
    residual_variance_present: bool,
    mapping_authority_established: bool,
    mapping_coverage: float | None,
    mapping_coverage_sufficient: bool,
    pair_coverage_sufficient: bool,
    stable_across_panels: bool,
) -> tuple[str, ...]:
    limitations = [
        (
            "observational panel association cannot establish causality or "
            "business contribution"
        ),
        "named-panel conclusions are outside this aggregate evidence envelope",
        (
            "cluster-robust uncertainty is not estimated; coefficients describe "
            "the observed aligned panel only"
        ),
    ]
    if not samples_met:
        limitations.append("minimum sample requirement was not met")
    if not panels_met:
        limitations.append("minimum panel requirement was not met")
    if not fe_converged:
        limitations.append("two-way fixed-effect residualization did not converge")
    if not residual_variance_present:
        limitations.append(
            "no usable residual variance remained after removing time and panel effects"
        )
    if not mapping_authority_established:
        limitations.append(
            "cross-source mapping authority is not established; result is "
            "sensitivity-only"
        )
    if mapping_coverage is None:
        limitations.append(
            "cross-source mapping coverage is unknown; result is sensitivity-only"
        )
    elif not mapping_coverage_sufficient:
        limitations.append(
            "cross-source mapping coverage is below the configured threshold"
        )
    if not pair_coverage_sufficient:
        limitations.append("complete-pair coverage is below the configured threshold")
    if residual_variance_present and not stable_across_panels:
        limitations.append(
            "association direction is not sufficiently stable across eligible panels"
        )
    return tuple(limitations)
