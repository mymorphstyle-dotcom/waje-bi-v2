from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope


def segment_contribution(
    rows: Iterable[dict[str, Any]],
    *,
    segment_key: str = "period",
    group_key: str = "group",
    target_group: str = "target",
    baseline_group: str = "baseline",
    amount_key: str = "amount",
    result_refs: tuple[str, ...] = (),
):
    pairs = {}
    skipped = 0
    for row in rows:
        segment = row.get(segment_key)
        group = row.get(group_key)
        amount = _number(row.get(amount_key))
        if segment in (None, "") or group is None or amount is None:
            skipped += 1
            continue
        pairs.setdefault(str(segment), {})[str(group)] = amount

    contributions = []
    for segment, groups in pairs.items():
        if baseline_group not in groups or target_group not in groups:
            skipped += 1
            continue
        baseline = groups[baseline_group]
        target = groups[target_group]
        delta = target - baseline
        contributions.append(
            {
                "segment": segment,
                "baseline_amount": baseline,
                "target_amount": target,
                "delta": delta,
                "delta_ratio": delta / abs(baseline) if baseline else None,
            }
        )

    total_delta = sum(item["delta"] for item in contributions)
    for item in contributions:
        item["delta_share"] = item["delta"] / total_delta if total_delta else 0.0
    contributions.sort(key=lambda item: item["delta"])
    dragged = [item for item in contributions if item["delta"] < 0]
    limitations = tuple(["skipped_incomplete_segments"] if skipped else [])
    if not contributions:
        limitations = ("no_comparable_segments",)
    return make_evidence_envelope(
        "segment_contribution",
        evidence_type="statistical_association" if contributions else "insufficient_evidence",
        strength="medium" if contributions else "low",
        wording_limit="contextual" if contributions else "insufficient",
        typed_payload={
            "segment_key": segment_key,
            "group_key": group_key,
            "total_delta": total_delta,
            "top_drags": tuple(dragged[:5]),
            "top_lifts": tuple(sorted(contributions, key=lambda item: item["delta"], reverse=True)[:5]),
            "segment_count": len(contributions),
            "skipped_rows_or_segments": skipped,
        },
        limitations=limitations,
        result_refs=result_refs,
    )


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
