from __future__ import annotations

import re
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
    aggregate_rows = []
    supported_indicator_rows = 0
    unsupported_indicator_rows = 0
    partial_explicit_fields = False
    for row in rows:
        if row.get(group_key) in (None, ""):
            continue
        amount = _number(row.get(amount_key)) or 0.0
        paid_users = _number(row.get(users_key)) or 0.0
        high_value_amount, high_value_paid_users, indicator_source, row_limitation = _high_value_totals(
            row,
            amount=amount,
            paid_users=paid_users,
            policy=policy,
        )
        aggregate_row = {
            "group": str(row.get(group_key, "")),
            "amount": amount,
            "paid_users": paid_users,
            "high_value_amount": high_value_amount,
            "high_value_paid_users": high_value_paid_users,
        }
        if row.get("bucket") not in (None, ""):
            aggregate_row["bucket"] = str(row.get("bucket"))
        if row_limitation:
            aggregate_row["row_limitation"] = row_limitation
            partial_explicit_fields = True
        if indicator_source:
            aggregate_row["indicator_source"] = indicator_source
            supported_indicator_rows += 1
        else:
            unsupported_indicator_rows += 1
        aggregate_rows.append(aggregate_row)
    aggregate_rows = tuple(aggregate_rows)
    total_amount = sum(row["amount"] for row in aggregate_rows)
    total_users = sum(row["paid_users"] for row in aggregate_rows)
    high_value_amount = sum(row["high_value_amount"] for row in aggregate_rows)
    high_value_users = sum(row["high_value_paid_users"] for row in aggregate_rows)
    if not aggregate_rows:
        return make_evidence_envelope(
            "high_value_user_contribution",
            evidence_type="insufficient_evidence",
            strength="low",
            wording_limit="insufficient",
            typed_payload={
                "privacy_policy": "aggregate_only",
                "threshold_policy": policy,
                "rows": aggregate_rows,
                "total_amount": total_amount,
                "total_paid_users": total_users,
                "high_value_amount": 0.0,
                "high_value_paid_users": 0.0,
                "high_value_amount_share": 0.0,
                "business_readout": "当前没有可用的高价值聚合结果。",
                "claim_boundary": "没有聚合结果时，不能写高价值用户贡献结论。",
            },
            limitations=("no_aggregate_high_value_rows",),
            result_refs=result_refs,
        )
    if partial_explicit_fields:
        return make_evidence_envelope(
            "high_value_user_contribution",
            evidence_type="insufficient_evidence",
            strength="low",
            wording_limit="insufficient",
            typed_payload={
                "privacy_policy": "aggregate_only",
                "threshold_policy": policy,
                "rows": aggregate_rows,
                "total_amount": total_amount,
                "total_paid_users": total_users,
                "high_value_amount": high_value_amount,
                "high_value_paid_users": high_value_users,
                "business_readout": "当前显式高价值聚合字段不完整，不能验证高价值金额和高价值用户数的统一口径。",
                "claim_boundary": "高价值聚合字段不完整时，不能写高价值分层贡献或金额占比结论。",
            },
            limitations=("partial_high_value_aggregate_fields",),
            result_refs=result_refs,
        )
    if supported_indicator_rows == 0:
        return make_evidence_envelope(
            "high_value_user_contribution",
            evidence_type="insufficient_evidence",
            strength="low",
            wording_limit="insufficient",
            typed_payload={
                "privacy_policy": "aggregate_only",
                "threshold_policy": policy,
                "rows": aggregate_rows,
                "total_amount": total_amount,
                "total_paid_users": total_users,
                "high_value_amount": 0.0,
                "high_value_paid_users": 0.0,
                "high_value_amount_share": 0.0,
                "business_readout": "当前聚合结果缺少可用的高价值指示字段，不能验证阈值口径下的高价值用户贡献。",
                "claim_boundary": "缺少高价值聚合标记时，只能保留总量观察，不能写成高价值用户贡献结论。",
            },
            limitations=("missing_high_value_indicator",),
            result_refs=result_refs,
        )
    limitations = ()
    if unsupported_indicator_rows:
        limitations = ("partial_high_value_indicator_coverage",)
    return make_evidence_envelope(
        "high_value_user_contribution",
        evidence_type="statistical_association",
        strength="medium",
        wording_limit="contextual",
        typed_payload={
            "privacy_policy": "aggregate_only",
            "threshold_policy": policy,
            "rows": aggregate_rows,
            "total_amount": total_amount,
            "total_paid_users": total_users,
            "high_value_amount": high_value_amount,
            "high_value_paid_users": high_value_users,
            "high_value_amount_share": (
                high_value_amount / total_amount if total_amount else 0.0
            ),
            "business_readout": _business_readout(
                policy,
                high_value_amount=high_value_amount,
                total_amount=total_amount,
                partial=bool(unsupported_indicator_rows),
            ),
            "claim_boundary": _claim_boundary(bool(unsupported_indicator_rows)),
        },
        limitations=limitations,
        result_refs=result_refs,
    )


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _high_value_totals(
    row: Mapping[str, Any],
    *,
    amount: float,
    paid_users: float,
    policy: Mapping[str, Any],
) -> tuple[float, float, str, str | None]:
    explicit_amount = _number(row.get("high_value_amount"))
    explicit_users = _number(row.get("high_value_paid_users"))
    if explicit_amount is not None or explicit_users is not None:
        if explicit_amount is None or explicit_users is None:
            return 0.0, 0.0, "", "partial_high_value_aggregate_fields"
        return explicit_amount, explicit_users, "embedded_totals", None

    high_value_flag = _boolish(row.get("is_high_value"))
    if high_value_flag is not None:
        flagged_amount, flagged_users = _flagged_totals(high_value_flag, amount, paid_users)
        return flagged_amount, flagged_users, "is_high_value", None

    percentile = _ratio(row.get("value_percentile"))
    threshold = _top_percentile_threshold(policy)
    if percentile is not None and threshold is not None:
        flagged_amount, flagged_users = _flagged_totals(percentile >= threshold, amount, paid_users)
        return flagged_amount, flagged_users, "value_percentile", None

    bucket_flag = _bucket_is_high_value(row.get("bucket"), policy)
    if bucket_flag is not None:
        flagged_amount, flagged_users = _flagged_totals(bucket_flag, amount, paid_users)
        return flagged_amount, flagged_users, "bucket", None

    return 0.0, 0.0, "", None


def _flagged_totals(is_high_value: bool, amount: float, paid_users: float) -> tuple[float, float]:
    if not is_high_value:
        return 0.0, 0.0
    return amount, paid_users


def _boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in {"true", "1", "yes", "y", "high_value", "high-value"}:
        return True
    if text in {"false", "0", "no", "n", "regular", "non_high_value", "non-high-value"}:
        return False
    return None


def _ratio(value: Any) -> float | None:
    numeric = _number(value)
    if numeric is None:
        return None
    if 1 < abs(numeric) <= 100:
        return numeric / 100
    return numeric


def _top_percentile_threshold(policy: Mapping[str, Any]) -> float | None:
    if str(policy.get("type") or "") != "top_percentile":
        return None
    return _ratio(policy.get("value"))


def _bucket_is_high_value(value: Any, policy: Mapping[str, Any]) -> bool | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    collapsed = text.replace("-", "_").replace(" ", "_")
    if collapsed in {"regular", "standard", "other", "low_value", "non_high_value"}:
        return False
    if any(token in collapsed for token in ("high_value", "vip", "whale")):
        return True
    threshold = _top_percentile_threshold(policy)
    if threshold is None:
        return None
    parsed = _bucket_percentile_match(collapsed)
    if parsed is not None:
        return True if parsed >= threshold else None
    return False if any(token in collapsed for token in ("mid", "regular", "base")) else None


def _bucket_percentile_match(collapsed: str) -> float | None:
    if match := re.search(r"(?:^|_)p(\d{1,3})(?:_plus|plus|th|th_percentile|percentile|$)", collapsed):
        return int(match.group(1)) / 100
    if match := re.search(r"(?:^|_)(\d{1,3})(?:th|st|nd|rd)?_percentile(?:$|_)", collapsed):
        return int(match.group(1)) / 100
    if match := re.search(r"(?:^|_)top_(\d{1,3})_percent(?:$|_)", collapsed):
        top_share = int(match.group(1))
        if 0 < top_share <= 100:
            return 1 - (top_share / 100)
    if match := re.search(r"(?:^|_)top(\d{1,3})pct(?:$|_)", collapsed):
        top_share = int(match.group(1))
        if 0 < top_share <= 100:
            return 1 - (top_share / 100)
    return None


def _business_readout(
    policy: Mapping[str, Any],
    *,
    high_value_amount: float,
    total_amount: float,
    partial: bool,
) -> str:
    share = (high_value_amount / total_amount) if total_amount else 0.0
    scope = "部分聚合行缺少高价值指示字段，当前只覆盖已标记分层。" if partial else "当前聚合结果包含可用的高价值指示字段。"
    return (
        f"{scope} 按阈值策略 {dict(policy)} 复核后，"
        f"高价值分层贡献占聚合金额的 {share:.1%}。"
    )


def _claim_boundary(partial: bool) -> str:
    if partial:
        return "只支持已标记聚合分层的高价值贡献判断，未标记部分不能外推到整体。"
    return "只支持聚合高价值分层贡献判断，不输出用户明细或个人支付记录。"
