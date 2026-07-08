from __future__ import annotations

from typing import Any, Iterable, Mapping

from bi_agent.capabilities import make_evidence_envelope


def high_value_user_contribution(
    rows: Iterable[dict[str, Any]],
    *,
    threshold_policy: Mapping[str, Any] | None = None,
    group_key: str = "group",
    amount_key: str = "amount",
    users_key: str = "paid_users",
    result_refs: tuple[str, ...] = (),
):
    policy = dict(threshold_policy or {"type": "top_percentile", "value": 0.95})
    aggregate_rows = tuple(
        {
            "group": str(row.get(group_key, "")),
            "amount": _number(row.get(amount_key)) or 0.0,
            "paid_users": _number(row.get(users_key)) or 0.0,
        }
        for row in rows
        if row.get(group_key) not in (None, "")
    )
    total_amount = sum(row["amount"] for row in aggregate_rows)
    total_users = sum(row["paid_users"] for row in aggregate_rows)
    return make_evidence_envelope(
        "high_value_user_contribution",
        evidence_type="statistical_association" if aggregate_rows else "insufficient_evidence",
        strength="medium" if aggregate_rows else "low",
        wording_limit="contextual" if aggregate_rows else "insufficient",
        typed_payload={
            "privacy_policy": "aggregate_only",
            "threshold_policy": policy,
            "rows": aggregate_rows,
            "total_amount": total_amount,
            "total_paid_users": total_users,
            "business_readout": "高价值用户贡献已按阈值策略汇总到聚合层。",
            "claim_boundary": "只能说明聚合高价值用户分层贡献，不输出用户明细或个人支付记录。",
        },
        limitations=() if aggregate_rows else ("no_aggregate_high_value_rows",),
        result_refs=result_refs,
    )


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
