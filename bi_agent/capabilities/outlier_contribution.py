from __future__ import annotations

from statistics import median
from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope


def outlier_contribution(
    rows: Iterable[dict[str, Any]],
    *,
    period_key: str = "period",
    period_grain: str = "period",
    group_key: str = "group",
    target_group: str = "target",
    baseline_group: str = "baseline",
    amount_key: str = "amount",
    top_n: int = 5,
    removal_policy: str = "",
    max_removed_periods: int | None = None,
    direction_after_removal: bool = True,
    result_refs: tuple[str, ...] = (),
):
    normalized_removal_policy = removal_policy or "top_positive_contribution_periods"
    if normalized_removal_policy != "top_positive_contribution_periods":
        return make_evidence_envelope(
            "outlier_contribution",
            evidence_type="insufficient_evidence",
            strength="low",
            wording_limit="insufficient",
            typed_payload={
                "period_grain": period_grain,
                "removal_policy": normalized_removal_policy,
                "direction_after_removal_evaluated": False,
                "business_readout": "当前只支持按最大正向贡献周期复算，暂时不能执行其他异常移除策略。",
                "claim_boundary": "未支持的异常移除策略不能进入业务结论。",
            },
            limitations=("unsupported_removal_policy",),
            result_refs=result_refs,
        )
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
    removed_limit = _positive_int(max_removed_periods, top_n)
    removed_positive = sorted(
        (item for item in deltas if item["delta"] > 0),
        key=lambda item: item["delta"],
        reverse=True,
    )[:removed_limit]
    remaining_delta = total_delta - sum(item["delta"] for item in removed_positive)
    direction_preserved = _same_direction(total_delta, remaining_delta)
    typed_payload = {
        "paired_periods": len(deltas),
        "total_delta": total_delta,
        "median_delta": center,
        "mad_delta": mad,
        "outliers": tuple(outliers),
        "top_positive_periods": tuple(top_positive),
        "top_positive_share": top_positive_share,
        "concentrated_in_top_periods": top_positive_share >= 0.5,
        "period_grain": period_grain,
        "removal_policy": normalized_removal_policy,
        "max_removed_periods": removed_limit,
        "remaining_delta_after_top_positive": remaining_delta,
        "removed_positive_periods": tuple(removed_positive),
        "direction_after_removal_evaluated": direction_after_removal,
        "business_readout": _business_readout(
            remaining_delta,
            direction_preserved,
            evaluate_direction=direction_after_removal,
        ),
        "claim_boundary": _claim_boundary(direction_after_removal),
    }
    if direction_after_removal:
        typed_payload["direction_preserved_after_top_positive"] = direction_preserved
        typed_payload["direction_after_removal"] = (
            "preserved" if direction_preserved else "changed"
        )
    return make_evidence_envelope(
        "outlier_contribution",
        evidence_type="statistical_association",
        strength="medium",
        wording_limit="contextual",
        typed_payload=typed_payload,
        limitations=(),
        result_refs=result_refs,
    )


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _same_direction(before, after):
    if before == 0:
        return after == 0
    return (before > 0 and after > 0) or (before < 0 and after < 0)


def _business_readout(remaining_delta, direction_preserved, *, evaluate_direction):
    direction = "仍为上升" if remaining_delta > 0 else "转为下降" if remaining_delta < 0 else "接近持平"
    if not evaluate_direction:
        return f"移除最大正向贡献周期后，剩余变化值为 {remaining_delta:.1f}，本轮未评估方向是否保持。"
    if direction_preserved:
        return f"移除最大正向贡献周期后，剩余变化方向{direction}。"
    return f"移除最大正向贡献周期后，剩余变化方向发生变化，当前{direction}。"


def _claim_boundary(evaluate_direction):
    if not evaluate_direction:
        return "这是异常敏感性复算，本轮只输出移除高贡献周期后的剩余变化值，不评价方向是否保持，也不能证明异常是原因。"
    return "这是异常敏感性复算，只能说明移除高贡献周期后的方向变化，不能证明异常是原因。"
