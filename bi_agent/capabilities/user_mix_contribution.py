from __future__ import annotations

from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope


def user_mix_contribution(
    rows: Iterable[dict[str, Any]],
    *,
    segment_key: str = "channel",
    user_grain_policy: str = "new_vs_returning",
    mix_key: str = "user_mix_bucket",
    group_key: str = "group",
    amount_key: str = "amount",
    users_key: str = "paid_users",
    result_refs: tuple[str, ...] = (),
):
    aggregate_rows = tuple(
        {
            "segment": str(row.get(segment_key, "")),
            "user_mix_bucket": str(row.get(mix_key, "")),
            "group": str(row.get(group_key, "")),
            "amount": _number(row.get(amount_key)) or 0.0,
            "paid_users": _number(row.get(users_key)) or 0.0,
        }
        for row in rows
        if row.get(segment_key) not in (None, "") and row.get(mix_key) not in (None, "")
    )
    total_amount = sum(row["amount"] for row in aggregate_rows)
    total_users = sum(row["paid_users"] for row in aggregate_rows)
    return make_evidence_envelope(
        "user_mix_contribution",
        evidence_type="dimension_localization"
        if aggregate_rows
        else "insufficient_evidence",
        strength="medium" if aggregate_rows else "low",
        wording_limit="contextual" if aggregate_rows else "insufficient",
        typed_payload={
            "privacy_policy": "aggregate_only",
            "segment_key": segment_key,
            "user_grain_policy": user_grain_policy,
            "rows": aggregate_rows,
            "segment_count": len({row["segment"] for row in aggregate_rows}),
            "mix_bucket_count": len({row["user_mix_bucket"] for row in aggregate_rows}),
            "total_amount": total_amount,
            "total_paid_users": total_users,
            "business_readout": "新老用户结构贡献已按聚合分群计算。",
            "claim_boundary": "只支持聚合分群层面的结构判断，不输出用户明细或个人级结论。",
        },
        limitations=() if aggregate_rows else ("no_aggregate_user_mix_rows",),
        result_refs=result_refs,
    )


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
