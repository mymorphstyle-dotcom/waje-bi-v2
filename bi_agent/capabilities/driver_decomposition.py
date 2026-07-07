from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope


def driver_decomposition(
    rows: Iterable[dict[str, Any]],
    *,
    period_key: str = "period",
    group_key: str = "group",
    target_group: str = "target",
    baseline_group: str = "baseline",
    amount_key: str = "amount",
    user_key: str = "paid_users",
    order_key: str = "orders",
    result_refs: tuple[str, ...] = (),
):
    grouped = _groups(rows, period_key, group_key)
    results = []
    for period, values in sorted(grouped.items()):
        baseline = values.get(baseline_group)
        target = values.get(target_group)
        if not baseline or not target:
            continue
        result = _decompose_pair(
            period,
            baseline,
            target,
            amount_key=amount_key,
            user_key=user_key,
            order_key=order_key,
        )
        if result:
            results.append(result)

    limitations = () if results else ("driver_components_missing",)
    return make_evidence_envelope(
        "driver_decomposition",
        evidence_type="accounting_contribution" if results else "insufficient_evidence",
        strength="high" if results else "low",
        wording_limit="quantified" if results else "insufficient",
        typed_payload={
            "decompositions": tuple(results),
            "primary_driver": results[0]["primary_driver"] if results else "",
            "volume_share": results[0]["volume_share"] if results else None,
            "unit_value_share": results[0]["unit_value_share"] if results else None,
            "amount_delta_ratio": results[0]["amount_delta_ratio"] if results else None,
        },
        limitations=limitations,
        result_refs=result_refs,
    )


def _decompose_pair(period, baseline, target, *, amount_key, user_key, order_key):
    baseline_amount = _number(baseline.get(amount_key))
    target_amount = _number(target.get(amount_key))
    if baseline_amount is None or target_amount is None:
        return None

    volume_key = user_key if _number(baseline.get(user_key)) is not None else order_key
    baseline_volume = _number(baseline.get(volume_key))
    target_volume = _number(target.get(volume_key))
    if not baseline_volume or not target_volume:
        return None

    baseline_unit = baseline_amount / baseline_volume
    target_unit = target_amount / target_volume
    amount_delta = target_amount - baseline_amount
    volume_effect = (target_volume - baseline_volume) * ((baseline_unit + target_unit) / 2)
    unit_effect = (target_unit - baseline_unit) * ((baseline_volume + target_volume) / 2)
    total_effect = volume_effect + unit_effect
    if total_effect:
        volume_share = volume_effect / total_effect
        unit_share = unit_effect / total_effect
    else:
        volume_share = 0.0
        unit_share = 0.0
    primary_driver = "volume" if abs(volume_effect) >= abs(unit_effect) else "unit_value"
    return {
        "period": period,
        "amount_delta": amount_delta,
        "amount_delta_ratio": _ratio(amount_delta, baseline_amount),
        "volume_key": volume_key,
        "baseline_volume": baseline_volume,
        "target_volume": target_volume,
        "baseline_unit_value": baseline_unit,
        "target_unit_value": target_unit,
        "volume_effect": volume_effect,
        "unit_value_effect": unit_effect,
        "volume_share": volume_share,
        "unit_value_share": unit_share,
        "primary_driver": primary_driver,
    }


def _groups(rows, period_key, group_key):
    grouped = {}
    for row in rows:
        period = row.get(period_key, "all")
        group = row.get(group_key)
        if group is None:
            continue
        grouped.setdefault(period, {})[group] = row
    return grouped


def _ratio(delta, baseline):
    if not baseline:
        return None
    return delta / abs(baseline)


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
