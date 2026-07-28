from __future__ import annotations

from math import isfinite
from typing import Any, Iterable, Mapping

from bi_agent.capabilities import make_evidence_envelope


def high_value_user_contribution(
    rows: Iterable[dict[str, Any]],
    *,
    threshold_policy: Mapping[str, Any],
    high_value_users_aggregation: str,
    group_key: str,
    total_amount_key: str,
    high_value_amount_key: str,
    high_value_users_key: str,
    threshold_key: str,
    result_refs: tuple[str, ...] = (),
):
    policy = dict(threshold_policy)
    threshold = _top_percentile_threshold(policy)
    if threshold is None or not 0 < threshold < 1:
        raise ValueError("high_value_threshold_policy_invalid")
    users_measure = _high_value_users_measure(high_value_users_aggregation)
    aggregate_rows = []
    observed_groups: set[str] = set()
    for row in rows:
        group = row.get(group_key)
        if group in (None, ""):
            raise ValueError("high_value_group_missing")
        normalized_group = str(group)
        if normalized_group in observed_groups:
            raise ValueError(f"high_value_group_duplicated:{normalized_group}")
        observed_groups.add(normalized_group)
        amount = _required_number(row, total_amount_key)
        high_value_amount = _required_number(row, high_value_amount_key)
        high_value_paid_users = _required_number(row, high_value_users_key)
        threshold_cutoff = _required_number(row, threshold_key)
        if amount < 0 or high_value_amount < 0 or high_value_paid_users < 0:
            raise ValueError("high_value_aggregate_negative")
        if amount >= 0 and high_value_amount > amount:
            raise ValueError("high_value_amount_exceeds_total")
        if (
            high_value_users_aggregation == "window_distinct_count"
            and not high_value_paid_users.is_integer()
        ):
            raise ValueError("high_value_paid_users_non_integral")
        normalized_high_value_paid_users: int | float = high_value_paid_users
        if high_value_users_aggregation == "window_distinct_count":
            normalized_high_value_paid_users = int(high_value_paid_users)
        aggregate_rows.append(
            {
                "group": normalized_group,
                "total_amount": amount,
                "high_value_amount": high_value_amount,
                "high_value_paid_users": normalized_high_value_paid_users,
                "high_value_threshold": threshold_cutoff,
                "high_value_amount_share": (
                    high_value_amount / amount if amount else 0.0
                ),
            }
        )
    aggregate_rows = tuple(aggregate_rows)
    if not aggregate_rows:
        return make_evidence_envelope(
            "high_value_user_contribution",
            evidence_type="insufficient_evidence",
            strength="low",
            wording_limit="insufficient",
            typed_payload={
                "privacy_policy": "aggregate_only",
                "threshold_policy": policy,
                "high_value_paid_users_measure": users_measure,
                "rows": aggregate_rows,
                "comparison_available": False,
                "business_readout": "当前没有可用的高价值聚合结果。",
                "claim_boundary": "没有聚合结果时，不能写高价值用户贡献结论。",
            },
            limitations=("no_aggregate_high_value_rows",),
            result_refs=result_refs,
        )
    by_group = {row["group"]: row for row in aggregate_rows}
    target = by_group.get("target")
    baseline = by_group.get("baseline")
    comparison = (
        {
            "baseline_share": baseline["high_value_amount_share"],
            "target_share": target["high_value_amount_share"],
            "share_delta": (
                target["high_value_amount_share"] - baseline["high_value_amount_share"]
            ),
        }
        if target is not None and baseline is not None
        else None
    )
    return make_evidence_envelope(
        "high_value_user_contribution",
        evidence_type="accounting_contribution",
        strength="medium",
        wording_limit="contextual",
        typed_payload={
            "privacy_policy": "aggregate_only",
            "threshold_policy": policy,
            "high_value_paid_users_measure": users_measure,
            "rows": aggregate_rows,
            "comparison_available": comparison is not None,
            "comparison": comparison,
            "business_readout": _business_readout(policy, comparison=comparison),
            "claim_boundary": _claim_boundary(),
        },
        limitations=(),
        result_refs=result_refs,
    )


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _required_number(row: Mapping[str, Any], key: str) -> float:
    if key not in row:
        raise ValueError(f"high_value_field_missing:{key}")
    value = _number(row[key])
    if value is None or not isfinite(value):
        raise ValueError(f"high_value_field_invalid:{key}")
    return value


def _top_percentile_threshold(policy: Mapping[str, Any]) -> float | None:
    if str(policy.get("type") or "") != "top_percentile":
        return None
    value = policy.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if isfinite(parsed) else None


def _high_value_users_measure(aggregation: str) -> str:
    if aggregation == "window_distinct_count":
        return "distinct_users_in_window"
    if aggregation == "mean_per_complete_day":
        return "average_users_per_complete_day"
    raise ValueError("high_value_users_aggregation_invalid")


def _business_readout(
    policy: Mapping[str, Any],
    *,
    comparison: Mapping[str, float] | None,
) -> str:
    if comparison is None:
        return f"当前聚合结果按阈值策略 {dict(policy)} 给出各窗口独立的高价值金额占比。"
    return (
        f"按阈值策略 {dict(policy)} 在各窗口内独立分层后，高价值金额占比从 "
        f"{comparison['baseline_share']:.1%} 变为 {comparison['target_share']:.1%}，"
        f"变化 {comparison['share_delta']:+.1%}。"
    )


def _claim_boundary() -> str:
    return (
        "只支持各窗口独立阈值下的聚合占比及其变化，不输出用户明细，也不把两个窗口的"
        "高价值分层解释成同一批稳定用户。"
    )
