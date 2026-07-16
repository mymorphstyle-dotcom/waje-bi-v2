from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope
from bi_agent.runtime.formula_claim_numbers import project_formula_claim_numbers


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
    target_window_id: str = "",
    baseline_window_id: str = "",
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
    summary = _claim_projection(results[0]) if results else {}
    primary = results[0] if results else {}
    return make_evidence_envelope(
        "driver_decomposition",
        evidence_type="accounting_contribution" if results else "insufficient_evidence",
        strength="high" if results else "low",
        wording_limit="quantified" if results else "insufficient",
        numeric_facts=(
            project_formula_claim_numbers(primary)
            if results
            else {}
        ),
        typed_payload={
            "decompositions": tuple(results),
            "primary_driver": results[0]["primary_driver"] if results else "",
            "volume_share": results[0]["volume_share"] if results else None,
            "unit_value_share": results[0]["unit_value_share"] if results else None,
            "amount_delta_ratio": results[0]["amount_delta_ratio"] if results else None,
            "metric": "paid_amount",
            "target_window_id": target_window_id if results else "",
            "baseline_window_id": baseline_window_id if results else "",
            "target_value": primary.get("target_amount"),
            "baseline_value": primary.get("baseline_amount"),
            "amount_delta": primary.get("amount_delta"),
            **summary,
        },
        limitations=limitations,
        result_refs=result_refs,
    )


def _claim_projection(decomposition):
    projection = {
        key: decomposition.get(key)
        for key in (
            "primary_core_driver",
            "core_driver_ranking",
            "core_factor_contributions",
            "core_factor_effect_total",
            "core_reconciliation_residual",
            "core_reconciliation_status",
            "payment_success_assumption",
        )
        if key in decomposition
    }
    for contribution in decomposition.get("core_factor_contributions") or ():
        component_id = str(contribution.get("component_id") or "")
        if not component_id:
            continue
        for field in (
            "baseline_value",
            "target_value",
            "delta",
            "delta_ratio",
            "contribution",
            "contribution_share",
        ):
            projection[f"{component_id}_{field}"] = contribution.get(field)
    for change in decomposition.get("component_changes") or ():
        component_id = str(change.get("component_id") or "")
        if not component_id or change.get("observed") is False:
            continue
        for field in ("baseline_value", "target_value", "delta", "delta_ratio"):
            projection.setdefault(f"{component_id}_{field}", change.get(field))
    return projection


def _decompose_pair(period, baseline, target, *, amount_key, user_key, order_key):
    amount_aliases = ("paid_amount",) if amount_key == "amount" else ()
    baseline_amount = _row_value(baseline, amount_key, *amount_aliases)
    target_amount = _row_value(target, amount_key, *amount_aliases)
    if baseline_amount is None or target_amount is None:
        return None

    volume_key = user_key if _row_value(baseline, user_key) is not None else order_key
    volume_aliases = ("paid_orders",) if volume_key == "orders" else ()
    baseline_volume = _row_value(baseline, volume_key, *volume_aliases)
    target_volume = _row_value(target, volume_key, *volume_aliases)
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
    core = _core_factor_decomposition(
        baseline,
        target,
        amount_delta=amount_delta,
    )
    result = {
        "period": period,
        "baseline_amount": baseline_amount,
        "target_amount": target_amount,
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
        "component_changes": _component_changes(baseline, target),
        "payment_success_assumption": _payment_success_assumption(
            baseline,
            target,
        ),
    }
    if core:
        result.update(core)
    return result


def _core_factor_decomposition(baseline, target, *, amount_delta):
    factor_values = (
        (
            "paid_users",
            _row_value(baseline, "paid_users"),
            _row_value(target, "paid_users"),
        ),
        (
            "paid_frequency",
            _paid_frequency(baseline),
            _paid_frequency(target),
        ),
        (
            "avg_order_amount",
            _avg_order_amount(baseline),
            _avg_order_amount(target),
        ),
    )
    if any(
        baseline_value is None or target_value is None
        for _, baseline_value, target_value in factor_values
    ):
        return {}

    baseline_values = tuple(item[1] for item in factor_values)
    target_values = tuple(item[2] for item in factor_values)
    effects = tuple(
        _three_factor_shapley_effect(
            factor_index=index,
            baseline_values=baseline_values,
            target_values=target_values,
        )
        for index in range(3)
    )
    total_effect = sum(effects)
    contributions = tuple(
        {
            "component_id": component_id,
            "baseline_value": baseline_value,
            "target_value": target_value,
            "delta": target_value - baseline_value,
            "delta_ratio": _ratio(
                target_value - baseline_value,
                baseline_value,
            ),
            "contribution": contribution,
            "contribution_share": (
                contribution / total_effect if total_effect else None
            ),
            "status": "observed",
        }
        for (
            component_id,
            baseline_value,
            target_value,
        ), contribution in zip(factor_values, effects)
    )
    ranking = tuple(
        item["component_id"]
        for item in sorted(
            contributions,
            key=lambda item: abs(item["contribution"]),
            reverse=True,
        )
    )
    residual = amount_delta - total_effect
    return {
        "core_factor_contributions": contributions,
        "core_driver_ranking": ranking,
        "primary_core_driver": ranking[0],
        "core_factor_effect_total": total_effect,
        "core_reconciliation_residual": residual,
        "core_reconciliation_status": (
            "reconciled"
            if abs(residual) <= max(1e-9, abs(amount_delta) * 1e-9)
            else "mismatch"
        ),
    }


def _three_factor_shapley_effect(
    *,
    factor_index,
    baseline_values,
    target_values,
):
    other_indexes = tuple(index for index in range(3) if index != factor_index)
    first, second = other_indexes
    marginal = target_values[factor_index] - baseline_values[factor_index]
    context = (
        baseline_values[first] * baseline_values[second] / 3.0
        + target_values[first] * baseline_values[second] / 6.0
        + baseline_values[first] * target_values[second] / 6.0
        + target_values[first] * target_values[second] / 3.0
    )
    return marginal * context


def _payment_success_assumption(baseline, target):
    baseline_value = _row_value(baseline, "payment_success_rate")
    target_value = _row_value(target, "payment_success_rate")
    if baseline_value is not None and target_value is not None:
        return None
    return {
        "component_id": "payment_success_rate",
        "status": "assumed_neutral",
        "baseline_value": 1.0,
        "target_value": 1.0,
        "contribution": 0.0,
        "observed": False,
    }


def _component_changes(baseline, target):
    components = (
        ("paid_users", "付费人数", _row_value(baseline, "paid_users"), _row_value(target, "paid_users")),
        (
            "orders",
            "付费订单数",
            _row_value(baseline, "orders", "paid_orders"),
            _row_value(target, "orders", "paid_orders"),
        ),
        (
            "paid_frequency",
            "付费频次",
            _paid_frequency(baseline),
            _paid_frequency(target),
        ),
        (
            "avg_order_amount",
            "单笔付费金额",
            _avg_order_amount(baseline),
            _avg_order_amount(target),
        ),
        (
            "first_paid_users",
            "首充人数",
            _row_value(baseline, "first_paid_users"),
            _row_value(target, "first_paid_users"),
        ),
        (
            "first_pay_user_share",
            "首充用户占比",
            _first_pay_user_share(baseline),
            _first_pay_user_share(target),
        ),
        (
            "payment_success_rate",
            "支付成功率",
            _row_value(baseline, "payment_success_rate"),
            _row_value(target, "payment_success_rate"),
        ),
    )
    changes = []
    for component_id, business_name, baseline_value, target_value in components:
        if baseline_value is None or target_value is None:
            if component_id == "payment_success_rate":
                changes.append(
                    {
                        "component_id": component_id,
                        "business_name": business_name,
                        "baseline_value": 1.0,
                        "target_value": 1.0,
                        "delta": 0.0,
                        "delta_ratio": 0.0,
                        "contribution": 0.0,
                        "status": "assumed_neutral",
                        "observed": False,
                    }
                )
            continue
        delta = target_value - baseline_value
        changes.append(
            {
                "component_id": component_id,
                "business_name": business_name,
                "baseline_value": baseline_value,
                "target_value": target_value,
                "delta": delta,
                "delta_ratio": _ratio(delta, baseline_value),
                "status": "observed",
                "observed": True,
            }
        )
    return tuple(changes)


def _row_value(row, key, *fallback_keys):
    for candidate in (key, *fallback_keys):
        value = _number(row.get(candidate))
        if value is not None:
            return value
    return None


def _paid_frequency(row):
    explicit = _row_value(row, "paid_frequency")
    if explicit is not None:
        return explicit
    orders = _row_value(row, "orders", "paid_orders")
    paid_users = _row_value(row, "paid_users")
    if orders is None or not paid_users:
        return None
    return orders / paid_users


def _avg_order_amount(row):
    explicit = _row_value(row, "avg_order_amount")
    if explicit is not None:
        return explicit
    amount = _row_value(row, "amount", "paid_amount")
    orders = _row_value(row, "orders", "paid_orders")
    if amount is None or not orders:
        return None
    return amount / orders


def _first_pay_user_share(row):
    explicit = _row_value(row, "first_pay_user_share")
    if explicit is not None:
        return explicit
    first_paid_users = _row_value(row, "first_paid_users")
    paid_users = _row_value(row, "paid_users")
    if first_paid_users is None or not paid_users:
        return None
    return first_paid_users / paid_users


def _groups(rows, period_key, group_key):
    grouped = {}
    for row in rows:
        group = row.get(group_key)
        authoritative_window_row = (
            group is None and row.get("window_role") not in (None, "")
        )
        if authoritative_window_row:
            group = row.get("window_role")
            period = "comparison_window"
        else:
            period = row.get(period_key, "all")
        if period in (None, "") or group is None:
            continue
        grouped.setdefault(str(period), {})[str(group)] = row
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
