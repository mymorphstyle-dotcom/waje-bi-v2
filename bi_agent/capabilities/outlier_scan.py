from math import isfinite
from statistics import median
from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope


def outlier_scan(
    rows: Iterable[dict[str, Any]],
    *,
    value_key: str = "amount",
    period_key: str = "observation_key",
    group_key: str = "window_role",
    target_group: str = "target",
    reference_group: str = "reference",
    min_reference_samples: int = 7,
    mad_threshold: float = 6.0,
    result_refs: tuple[str, ...] = (),
):
    if (
        isinstance(min_reference_samples, bool)
        or not isinstance(min_reference_samples, int)
        or min_reference_samples < 3
    ):
        raise ValueError("outlier_scan_min_reference_samples_invalid")
    threshold = _as_number(mad_threshold)
    if threshold is None or threshold <= 0:
        raise ValueError("outlier_scan_mad_threshold_invalid")

    target_observations = []
    reference_observations = []
    excluded_other_groups = 0
    observed_periods = {target_group: set(), reference_group: set()}
    for row in rows:
        group = row.get(group_key)
        if group not in observed_periods:
            excluded_other_groups += 1
            continue
        raw_period = row.get(period_key)
        period = str(raw_period).strip() if raw_period is not None else ""
        if not period:
            raise ValueError(f"outlier_scan_{group}_period_missing")
        if period in observed_periods[group]:
            raise ValueError(f"outlier_scan_{group}_period_duplicated:{period}")
        value = _as_number(row.get(value_key))
        if value is None:
            raise ValueError(f"outlier_scan_{group}_value_invalid:{period}")
        observed_periods[group].add(period)
        observation = {"period": period, "amount": value}
        if group == target_group:
            target_observations.append(observation)
        else:
            reference_observations.append(observation)

    boundary_payload = {
        "target_period_count": len(target_observations),
        "reference_period_count": len(reference_observations),
        "excluded_other_group_period_count": excluded_other_groups,
        "target_group": target_group,
        "reference_group": reference_group,
        "reference_distribution_policy": "preceding_complete_daily_observations",
        "minimum_reference_samples": min_reference_samples,
        "mad_threshold": threshold,
        "period_grain": "day",
        "claim_ceiling": "anomaly_candidate",
        "causal_claim_allowed": False,
    }
    if not target_observations:
        return make_evidence_envelope(
            "outlier_scan",
            evidence_type="insufficient_evidence",
            strength="low",
            wording_limit="insufficient",
            typed_payload={
                **boundary_payload,
                "outliers": (),
                "value_count": 0,
            },
            limitations=("target_daily_values_missing",),
            result_refs=result_refs,
        )
    if len(reference_observations) < min_reference_samples:
        return make_evidence_envelope(
            "outlier_scan",
            evidence_type="insufficient_evidence",
            strength="low",
            wording_limit="insufficient",
            typed_payload={
                **boundary_payload,
                "outliers": (),
                "value_count": len(target_observations),
            },
            limitations=("insufficient_reference_daily_values",),
            result_refs=result_refs,
        )

    reference_values = [item["amount"] for item in reference_observations]
    center = median(reference_values)
    deviations = [abs(value - center) for value in reference_values]
    mad = median(deviations)
    if mad == 0:
        outliers = tuple(
            {
                "period": item["period"],
                "target_amount": item["amount"],
                "absolute_deviation": abs(item["amount"] - center),
                "mad_multiple": None,
            }
            for item in target_observations
            if item["amount"] != center
        )
    else:
        outliers = tuple(
            {
                "period": item["period"],
                "target_amount": item["amount"],
                "absolute_deviation": abs(item["amount"] - center),
                "mad_multiple": abs(item["amount"] - center) / mad,
            }
            for item in target_observations
            if abs(item["amount"] - center) / mad > threshold
        )
    return make_evidence_envelope(
        "outlier_scan",
        evidence_type="statistical_association",
        strength="medium" if outliers else "low",
        wording_limit="candidate" if outliers else "none_found",
        typed_payload={
            **boundary_payload,
            "outliers": outliers,
            "value_count": len(target_observations),
            "reference_median": center,
            "reference_mad": mad,
        },
        limitations=(),
        result_refs=result_refs,
    )


scan_outliers = outlier_scan


def _as_number(value):
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None
