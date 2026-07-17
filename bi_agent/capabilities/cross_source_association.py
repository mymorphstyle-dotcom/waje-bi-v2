from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from math import copysign, exp, isfinite, lgamma, log, log1p, sqrt
from statistics import median
from typing import Any

from bi_agent.capabilities import make_evidence_envelope


_SUPPORTED_METHODS = frozenset({"pearson", "spearman"})
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
_SUPPORTED_FDR_METHODS = frozenset({"bh", "by"})


def cross_source_association(
    rows: Iterable[Mapping[str, Any]],
    *,
    target_key: str,
    candidate_keys: Sequence[str] | None = None,
    time_key: str = "business_date",
    methods: Sequence[str] = ("pearson", "spearman"),
    transforms: Sequence[str] = (
        "level",
        "difference",
        "signed_log_difference",
    ),
    lags: Sequence[int] = (0,),
    min_samples: int = 30,
    rolling_window: int = 90,
    rolling_step: int | None = None,
    min_rolling_windows: int = 3,
    stability_direction_ratio: float = 0.70,
    min_abs_correlation: float = 0.10,
    alpha: float = 0.05,
    fdr_method: str = "by",
    result_refs: tuple[str, ...] = (),
    evidence_ref: str | None = None,
):
    """Test observational associations between one target and candidate series.

    A positive lag means that the candidate precedes the target by that many
    aligned observations: ``target[t]`` is compared with ``candidate[t-lag]``.
    The function consumes already aligned aggregate time-series rows and does
    not read a database or infer a causal effect.
    """

    prepared_rows = _prepare_rows(rows, time_key=time_key)
    candidates = _candidate_keys(
        prepared_rows,
        target_key=target_key,
        time_key=time_key,
        requested=candidate_keys,
    )
    selected_methods = _normalize_methods(methods)
    selected_transforms = _normalize_transforms(transforms)
    selected_lags = _normalize_lags(lags)
    _validate_configuration(
        min_samples=min_samples,
        rolling_window=rolling_window,
        rolling_step=rolling_step,
        min_rolling_windows=min_rolling_windows,
        stability_direction_ratio=stability_direction_ratio,
        min_abs_correlation=min_abs_correlation,
        alpha=alpha,
        fdr_method=fdr_method,
    )
    rolling_step = rolling_step or max(1, rolling_window // 3)

    target = tuple(_number(row.get(target_key)) for row in prepared_rows)
    target_by_transform = {
        transform: _transform(target, transform) for transform in selected_transforms
    }
    estimates: list[dict[str, Any]] = []
    for candidate_key in candidates:
        candidate = tuple(
            _number(row.get(candidate_key)) for row in prepared_rows
        )
        candidate_by_transform = {
            transform: _transform(candidate, transform)
            for transform in selected_transforms
        }
        for transform in selected_transforms:
            for lag in selected_lags:
                pairs = _lagged_pairs(
                    target_by_transform[transform],
                    candidate_by_transform[transform],
                    lag,
                )
                for method in selected_methods:
                    estimates.append(
                        _estimate(
                            pairs,
                            candidate_key=candidate_key,
                            transform=transform,
                            method=method,
                            lag=lag,
                            min_samples=min_samples,
                            rolling_window=rolling_window,
                            rolling_step=rolling_step,
                            min_rolling_windows=min_rolling_windows,
                            stability_direction_ratio=stability_direction_ratio,
                            min_abs_correlation=min_abs_correlation,
                            alpha=alpha,
                        )
                    )

    valid_indexes = [
        index
        for index, estimate in enumerate(estimates)
        if estimate["p_value"] is not None
    ]
    adjusted = _adjust_p_values(
        [float(estimates[index]["p_value"]) for index in valid_indexes],
        method=fdr_method,
    )
    for index, q_value in zip(valid_indexes, adjusted):
        estimates[index]["q_value"] = q_value
        estimates[index]["fdr_significant"] = q_value <= alpha
        estimates[index]["supported"] = (
            q_value <= alpha
            and abs(float(estimates[index]["coefficient"]))
            >= min_abs_correlation
        )

    supported = [estimate for estimate in estimates if estimate["supported"]]
    stable = [estimate for estimate in supported if estimate["rolling"]["stable"]]
    robust_stable = [
        estimate for estimate in stable if estimate["transform"] != "level"
    ]
    ranked = sorted(
        supported,
        key=lambda estimate: (
            estimate["transform"] != "level",
            bool(estimate["rolling"]["stable"]),
            estimate["rolling"]["same_direction_ratio"],
            abs(float(estimate["coefficient"])),
            -float(estimate["q_value"]),
            estimate["sample_size"],
        ),
        reverse=True,
    )
    best = ranked[0] if ranked else None

    evidence_type, strength, wording_limit = _evidence_boundary(
        estimates=estimates,
        supported=supported,
        stable=stable,
        robust_stable=robust_stable,
    )
    limitations = _limitations(
        prepared_rows=prepared_rows,
        target=target,
        estimates=estimates,
        supported=supported,
        stable=stable,
        robust_stable=robust_stable,
        fdr_method=fdr_method,
    )
    numeric_facts: dict[str, Any] = {
        "aligned_row_count": len(prepared_rows),
        "tested_hypothesis_count": len(valid_indexes),
        "supported_association_count": len(supported),
        "stable_association_count": len(stable),
        "robust_stable_association_count": len(robust_stable),
    }
    if best is not None:
        numeric_facts.update(
            {
                "best_correlation": best["coefficient"],
                "best_lag": best["lag"],
                "best_q_value": best["q_value"],
                "best_sample_size": best["sample_size"],
            }
        )

    return make_evidence_envelope(
        "cross_source_association",
        evidence_ref=evidence_ref,
        evidence_type=evidence_type,
        strength=strength,
        wording_limit=wording_limit,
        numeric_facts=numeric_facts,
        typed_payload={
            "analysis_role": "cross_source_association",
            "target_key": target_key,
            "candidate_keys": candidates,
            "time_key": time_key,
            "methods": selected_methods,
            "transforms": selected_transforms,
            "lags": selected_lags,
            "lag_semantics": (
                "positive_lag_means_candidate_precedes_target_by_aligned_observations"
            ),
            "minimum_samples": min_samples,
            "rolling_window": rolling_window,
            "rolling_step": rolling_step,
            "minimum_rolling_windows": min_rolling_windows,
            "multiple_testing": {
                "method": fdr_method,
                "alpha": alpha,
                "family_scope": "all_valid_candidate_transform_lag_method_tests",
                "dependency_policy": (
                    "positive_or_arbitrary_dependence_conservative"
                    if fdr_method == "by"
                    else "independent_or_positive_dependence"
                ),
            },
            "causal_claim_allowed": False,
            "claim_ceiling": "stable_statistical_association",
            "coefficient_is_contribution": False,
            "confounding_ruled_out": False,
            "best_association": best,
            "supported_associations": tuple(ranked),
            "estimates": tuple(estimates),
        },
        limitations=limitations,
        result_refs=result_refs,
    )


def _prepare_rows(
    rows: Iterable[Mapping[str, Any]], *, time_key: str
) -> tuple[Mapping[str, Any], ...]:
    prepared = tuple(rows)
    if any(not isinstance(row, Mapping) for row in prepared):
        raise TypeError("rows must contain mappings")
    missing_time = [index for index, row in enumerate(prepared) if time_key not in row]
    if missing_time:
        raise ValueError(f"time_key is missing from rows: {missing_time[:5]}")
    time_values = [row[time_key] for row in prepared]
    if len({_hashable_time(value) for value in time_values}) != len(time_values):
        raise ValueError("time_key values must be unique")
    return tuple(sorted(prepared, key=lambda row: _time_sort_key(row[time_key])))


def _hashable_time(value: Any) -> tuple[str, str]:
    return type(value).__name__, repr(value)


def _time_sort_key(value: Any) -> tuple[int, Any]:
    if isinstance(value, (datetime, date)):
        return 0, value.isoformat()
    if isinstance(value, bool):
        return 2, str(value)
    if isinstance(value, (int, float)) and isfinite(float(value)):
        return 1, float(value)
    return 2, str(value)


def _candidate_keys(
    rows: tuple[Mapping[str, Any], ...],
    *,
    target_key: str,
    time_key: str,
    requested: Sequence[str] | None,
) -> tuple[str, ...]:
    if not target_key or target_key == time_key:
        raise ValueError("target_key must identify a metric column")
    if requested is None:
        discovered: list[str] = []
        for row in rows:
            for key, value in row.items():
                if key in {target_key, time_key} or key in discovered:
                    continue
                if _number(value) is not None:
                    discovered.append(str(key))
        candidates = tuple(discovered)
    else:
        candidates = tuple(dict.fromkeys(str(key) for key in requested if str(key)))
    if target_key in candidates or time_key in candidates:
        raise ValueError("candidate_keys cannot contain target_key or time_key")
    if not candidates:
        raise ValueError("at least one candidate metric is required")
    return candidates


def _normalize_methods(methods: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(str(method).lower() for method in methods))
    unsupported = set(normalized) - _SUPPORTED_METHODS
    if unsupported or not normalized:
        raise ValueError(f"unsupported association methods: {sorted(unsupported)}")
    return normalized


def _normalize_transforms(transforms: Sequence[str]) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys(str(transform).lower() for transform in transforms))
    unsupported = set(requested) - set(_TRANSFORM_ALIASES)
    if unsupported or not requested:
        raise ValueError(f"unsupported transforms: {sorted(unsupported)}")
    return tuple(dict.fromkeys(_TRANSFORM_ALIASES[item] for item in requested))


def _normalize_lags(lags: Sequence[int]) -> tuple[int, ...]:
    if any(isinstance(lag, bool) or not isinstance(lag, int) for lag in lags):
        raise ValueError("lags must be integer observation offsets")
    normalized = tuple(dict.fromkeys(lags))
    if not normalized:
        raise ValueError("at least one lag is required")
    return normalized


def _validate_configuration(
    *,
    min_samples: int,
    rolling_window: int,
    rolling_step: int | None,
    min_rolling_windows: int,
    stability_direction_ratio: float,
    min_abs_correlation: float,
    alpha: float,
    fdr_method: str,
) -> None:
    if min_samples < 3:
        raise ValueError("min_samples must be at least 3")
    if rolling_window < 3:
        raise ValueError("rolling_window must be at least 3")
    if rolling_step is not None and rolling_step < 1:
        raise ValueError("rolling_step must be positive")
    if min_rolling_windows < 1:
        raise ValueError("min_rolling_windows must be positive")
    if not 0.0 <= stability_direction_ratio <= 1.0:
        raise ValueError("stability_direction_ratio must be between 0 and 1")
    if not 0.0 <= min_abs_correlation <= 1.0:
        raise ValueError("min_abs_correlation must be between 0 and 1")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be between 0 and 1")
    if fdr_method not in _SUPPORTED_FDR_METHODS:
        raise ValueError(f"unsupported fdr_method: {fdr_method}")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _transform(
    values: tuple[float | None, ...], transform: str
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


def _lagged_pairs(
    target: tuple[float | None, ...],
    candidate: tuple[float | None, ...],
    lag: int,
) -> tuple[tuple[float, float], ...]:
    pairs: list[tuple[float, float]] = []
    for target_index, target_value in enumerate(target):
        candidate_index = target_index - lag
        if not 0 <= candidate_index < len(candidate):
            continue
        candidate_value = candidate[candidate_index]
        if target_value is None or candidate_value is None:
            continue
        pairs.append((target_value, candidate_value))
    return tuple(pairs)


def _estimate(
    pairs: tuple[tuple[float, float], ...],
    *,
    candidate_key: str,
    transform: str,
    method: str,
    lag: int,
    min_samples: int,
    rolling_window: int,
    rolling_step: int,
    min_rolling_windows: int,
    stability_direction_ratio: float,
    min_abs_correlation: float,
    alpha: float,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "candidate_key": candidate_key,
        "transform": transform,
        "method": method,
        "lag": lag,
        "sample_size": len(pairs),
        "coefficient": None,
        "p_value": None,
        "q_value": None,
        "raw_significant": False,
        "fdr_significant": False,
        "supported": False,
        "status": "insufficient_samples",
        "rolling": _empty_rolling(),
    }
    if len(pairs) < min_samples:
        return base
    coefficient = _correlation(pairs, method)
    if coefficient is None:
        base["status"] = "constant_series"
        return base
    p_value = _correlation_p_value(coefficient, len(pairs))
    base.update(
        {
            "coefficient": coefficient,
            "p_value": p_value,
            "raw_significant": p_value <= alpha,
            "status": "ok",
            "rolling": _rolling_stability(
                pairs,
                method=method,
                full_coefficient=coefficient,
                rolling_window=rolling_window,
                rolling_step=rolling_step,
                min_rolling_windows=min_rolling_windows,
                stability_direction_ratio=stability_direction_ratio,
                min_abs_correlation=min_abs_correlation,
                alpha=alpha,
            ),
        }
    )
    return base


def _empty_rolling() -> dict[str, Any]:
    return {
        "window_count": 0,
        "same_direction_ratio": 0.0,
        "median_correlation": None,
        "median_absolute_correlation": None,
        "raw_significant_window_ratio": 0.0,
        "stable": False,
    }


def _rolling_stability(
    pairs: tuple[tuple[float, float], ...],
    *,
    method: str,
    full_coefficient: float,
    rolling_window: int,
    rolling_step: int,
    min_rolling_windows: int,
    stability_direction_ratio: float,
    min_abs_correlation: float,
    alpha: float,
) -> dict[str, Any]:
    if len(pairs) < rolling_window:
        return _empty_rolling()
    last_start = len(pairs) - rolling_window
    starts = list(range(0, last_start + 1, rolling_step))
    if starts[-1] != last_start:
        starts.append(last_start)
    coefficients: list[float] = []
    significant = 0
    for start in starts:
        window = pairs[start : start + rolling_window]
        coefficient = _correlation(window, method)
        if coefficient is None:
            continue
        coefficients.append(coefficient)
        if _correlation_p_value(coefficient, len(window)) <= alpha:
            significant += 1
    if not coefficients:
        return _empty_rolling()
    direction = 1 if full_coefficient >= 0.0 else -1
    same_direction_ratio = sum(
        1 for coefficient in coefficients if coefficient * direction > 0.0
    ) / len(coefficients)
    median_correlation = median(coefficients)
    median_absolute = median(abs(value) for value in coefficients)
    return {
        "window_count": len(coefficients),
        "same_direction_ratio": same_direction_ratio,
        "median_correlation": median_correlation,
        "median_absolute_correlation": median_absolute,
        "raw_significant_window_ratio": significant / len(coefficients),
        "stable": (
            len(coefficients) >= min_rolling_windows
            and same_direction_ratio >= stability_direction_ratio
            and median_absolute >= min_abs_correlation
        ),
    }


def _correlation(
    pairs: tuple[tuple[float, float], ...], method: str
) -> float | None:
    target = [pair[0] for pair in pairs]
    candidate = [pair[1] for pair in pairs]
    if method == "spearman":
        target = _ranks(target)
        candidate = _ranks(candidate)
    target_mean = sum(target) / len(target)
    candidate_mean = sum(candidate) / len(candidate)
    target_deviation = [value - target_mean for value in target]
    candidate_deviation = [value - candidate_mean for value in candidate]
    target_variance = sum(value * value for value in target_deviation)
    candidate_variance = sum(value * value for value in candidate_deviation)
    if target_variance <= 0.0 or candidate_variance <= 0.0:
        return None
    coefficient = sum(
        left * right
        for left, right in zip(target_deviation, candidate_deviation)
    ) / sqrt(target_variance * candidate_variance)
    return max(-1.0, min(1.0, coefficient))


def _ranks(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(indexed):
        end = index + 1
        while end < len(indexed) and indexed[end][1] == indexed[index][1]:
            end += 1
        average_rank = (index + 1 + end) / 2.0
        for original_index, _ in indexed[index:end]:
            ranks[original_index] = average_rank
        index = end
    return ranks


def _correlation_p_value(coefficient: float, sample_size: int) -> float:
    if sample_size <= 2:
        return 1.0
    absolute = abs(coefficient)
    if absolute >= 1.0 - 1e-15:
        return 0.0
    degrees_of_freedom = sample_size - 2
    t_squared = (absolute * absolute * degrees_of_freedom) / (
        1.0 - absolute * absolute
    )
    x = degrees_of_freedom / (degrees_of_freedom + t_squared)
    return max(
        0.0,
        min(1.0, _regularized_incomplete_beta(x, degrees_of_freedom / 2.0, 0.5)),
    )


def _regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = exp(
        lgamma(a + b)
        - lgamma(a)
        - lgamma(b)
        + a * log(x)
        + b * log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_continued_fraction(a, b, x) / a
    return 1.0 - front * _beta_continued_fraction(b, a, 1.0 - x) / b


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    max_iterations = 200
    epsilon = 3e-14
    minimum = 1e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (minimum if abs(d) < minimum else d)
    result = d
    for iteration in range(1, max_iterations + 1):
        twice = 2 * iteration
        numerator = iteration * (b - iteration) * x / (
            (qam + twice) * (a + twice)
        )
        d = 1.0 + numerator * d
        d = minimum if abs(d) < minimum else d
        c = 1.0 + numerator / c
        c = minimum if abs(c) < minimum else c
        d = 1.0 / d
        result *= d * c
        numerator = -(a + iteration) * (qab + iteration) * x / (
            (a + twice) * (qap + twice)
        )
        d = 1.0 + numerator * d
        d = minimum if abs(d) < minimum else d
        c = 1.0 + numerator / c
        c = minimum if abs(c) < minimum else c
        d = 1.0 / d
        delta = d * c
        result *= delta
        if abs(delta - 1.0) < epsilon:
            break
    return result


def _adjust_p_values(p_values: Sequence[float], *, method: str) -> tuple[float, ...]:
    if method not in _SUPPORTED_FDR_METHODS:
        raise ValueError(f"unsupported fdr_method: {method}")
    if not p_values:
        return ()
    count = len(p_values)
    dependency_multiplier = (
        sum(1.0 / rank for rank in range(1, count + 1)) if method == "by" else 1.0
    )
    ordered = sorted(enumerate(p_values), key=lambda item: item[1])
    adjusted = [1.0] * count
    running_minimum = 1.0
    for reverse_index in range(count - 1, -1, -1):
        original_index, p_value = ordered[reverse_index]
        rank = reverse_index + 1
        candidate = min(1.0, float(p_value) * count * dependency_multiplier / rank)
        running_minimum = min(running_minimum, candidate)
        adjusted[original_index] = running_minimum
    return tuple(adjusted)


def _evidence_boundary(
    *,
    estimates: list[dict[str, Any]],
    supported: list[dict[str, Any]],
    stable: list[dict[str, Any]],
    robust_stable: list[dict[str, Any]],
) -> tuple[str, str, str]:
    if robust_stable:
        return "statistical_association", "medium", "stable_association"
    if supported:
        return "statistical_association", "low", "candidate_association"
    if any(estimate["status"] == "ok" for estimate in estimates):
        return "insufficient_evidence", "low", "exploratory_only"
    return "insufficient_evidence", "insufficient", "insufficient"


def _limitations(
    *,
    prepared_rows: tuple[Mapping[str, Any], ...],
    target: tuple[float | None, ...],
    estimates: list[dict[str, Any]],
    supported: list[dict[str, Any]],
    stable: list[dict[str, Any]],
    robust_stable: list[dict[str, Any]],
    fdr_method: str,
) -> tuple[str, ...]:
    limitations = {
        "observational_association_only",
        "confounding_not_ruled_out",
        "correlation_coefficient_is_not_contribution",
        "serial_dependence_not_adjusted_in_analytic_p_values",
    }
    if not prepared_rows:
        limitations.add("no_aligned_rows")
    elif any(value is None for value in target):
        limitations.add("target_missing_values")
    if any(estimate["status"] == "insufficient_samples" for estimate in estimates):
        limitations.add("some_hypotheses_below_minimum_samples")
    if any(estimate["status"] == "constant_series" for estimate in estimates):
        limitations.add("some_hypotheses_have_constant_series")
    if not supported:
        limitations.add("no_association_passed_effect_and_fdr_thresholds")
    elif not stable:
        limitations.add("rolling_stability_not_established")
    elif not robust_stable:
        limitations.add("stable_result_present_only_in_levels_and_may_reflect_trend")
    if fdr_method == "bh":
        limitations.add("bh_assumes_independent_or_positive_dependence")
    return tuple(sorted(limitations))
