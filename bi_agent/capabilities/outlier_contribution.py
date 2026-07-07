from statistics import median
from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope


def outlier_contribution(
    rows: Iterable[dict[str, Any]],
    *,
    period_key: str = "period",
    group_key: str = "group",
    target_group: str = "target",
    baseline_group: str = "baseline",
    amount_key: str = "amount",
    top_n: int = 5,
    result_refs: tuple[str, ...] = (),
):
    pairs = {}
    for row in rows:
        period = row.get(period_key)
        group = row.get(group_key)
        amount = _number(row.get(amount_key))
        if period is None or group is None or amount is None:
            continue
        pairs.setdefault(str(period), {})[str(group)] = amount

    deltas = []
    for period, groups in pairs.items():
        if baseline_group not in groups or target_group not in groups:
            continue
        deltas.append(
            {
                "period": period,
                "baseline_amount": groups[baseline_group],
                "target_amount": groups[target_group],
                "delta": groups[target_group] - groups[baseline_group],
            }
        )
    if not deltas:
        return make_evidence_envelope(
            "outlier_contribution",
            evidence_type="insufficient_evidence",
            strength="low",
            wording_limit="insufficient",
            typed_payload={"paired_periods": 0},
            limitations=("no_paired_periods",),
            result_refs=result_refs,
        )

    total_delta = sum(item["delta"] for item in deltas)
    center = median(item["delta"] for item in deltas)
    mad = median(abs(item["delta"] - center) for item in deltas)
    threshold = (6 * mad) if mad else 0
    outliers = [
        item
        for item in deltas
        if threshold and abs(item["delta"] - center) > threshold
    ]
    top_positive = sorted(deltas, key=lambda item: item["delta"], reverse=True)[:top_n]
    top_positive_delta = sum(max(0.0, item["delta"]) for item in top_positive)
    positive_delta = sum(max(0.0, item["delta"]) for item in deltas)
    top_positive_share = top_positive_delta / positive_delta if positive_delta else 0.0
    return make_evidence_envelope(
        "outlier_contribution",
        evidence_type="statistical_association",
        strength="medium",
        wording_limit="contextual",
        typed_payload={
            "paired_periods": len(deltas),
            "total_delta": total_delta,
            "median_delta": center,
            "mad_delta": mad,
            "outliers": tuple(outliers),
            "top_positive_periods": tuple(top_positive),
            "top_positive_share": top_positive_share,
            "concentrated_in_top_periods": top_positive_share >= 0.5,
        },
        limitations=(),
        result_refs=result_refs,
    )


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
