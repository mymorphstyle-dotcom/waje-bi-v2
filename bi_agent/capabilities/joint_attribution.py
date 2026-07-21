from typing import Any, Iterable, Optional

from bi_agent.capabilities import make_evidence_envelope
from bi_agent.capabilities.segment_bridge import (
    SPARSE_THRESHOLD,
    _has_sensitive_keys,
    _sample_size,
)


def joint_attribution(
    rows: Iterable[dict[str, Any]] = (),
    *,
    segment_evidence: Optional[Any] = None,
    dimension_keys: tuple[str, ...] = (),
    group_key: str = "group",
    target_group: str = "target",
    baseline_group: str = "baseline",
    amount_key: str = "amount",
    residual: float = 0.0,
    fit: float = 1.0,
    force_run: bool = False,
    result_refs: tuple[str, ...] = (),
):
    rows = tuple(rows)
    if segment_evidence is not None:
        payload = (
            segment_evidence.get("typed_payload", {})
            if isinstance(segment_evidence, dict)
            else getattr(segment_evidence, "typed_payload", {})
        )
        residual = payload.get("residual", residual)
        fit = payload.get("fit", fit)
    needs_escalation = (
        force_run or segment_evidence is None or abs(residual) > 0.10 or fit < 0.80
    )
    dimension_keys = dimension_keys or _infer_dimension_keys(
        rows, group_key, amount_key
    )
    if segment_evidence is None and (not rows or len(dimension_keys) < 2):
        return make_evidence_envelope(
            "joint_attribution",
            evidence_type="insufficient_evidence",
            strength="low",
            wording_limit="blocked",
            typed_payload={
                "dimension_keys": dimension_keys,
                "residual": residual,
                "fit": fit,
            },
            limitations=("segment_bridge_required",),
            result_refs=result_refs,
        )
    if not needs_escalation:
        return make_evidence_envelope(
            "joint_attribution",
            evidence_type="insufficient_evidence",
            strength="insufficient",
            wording_limit="insufficient",
            typed_payload={
                "dimension_keys": dimension_keys,
                "residual": residual,
                "fit": fit,
                "reason": "no_escalation_required",
            },
            limitations=("joint_attribution_not_required",),
            result_refs=result_refs,
        )

    if len(dimension_keys) < 2:
        return make_evidence_envelope(
            "joint_attribution",
            evidence_type="insufficient_evidence",
            strength="low",
            wording_limit="insufficient",
            typed_payload={
                "dimension_keys": dimension_keys,
                "residual": residual,
                "fit": fit,
            },
            limitations=("joint_dimensions_required",),
            result_refs=result_refs,
        )

    has_sensitive = any(_has_sensitive_keys(row) for row in rows)
    sample_sizes = tuple(_sample_size(row) for row in rows)
    has_unverified_sample = any(size is None for size in sample_sizes)
    safe_rows = tuple(
        row
        for row, size in zip(rows, sample_sizes)
        if size is None or size >= SPARSE_THRESHOLD
    )
    skipped_sparse = len(rows) - len(safe_rows)
    if has_sensitive or has_unverified_sample:
        limitations = tuple(
            reason
            for reason, present in (
                ("raw_identifier_present", has_sensitive),
                ("sparse_cell", bool(skipped_sparse)),
                ("sample_size_unverified", has_unverified_sample),
            )
            if present
        )
        return make_evidence_envelope(
            "joint_attribution",
            evidence_type="insufficient_evidence",
            strength="insufficient",
            wording_limit="blocked",
            typed_payload={
                "dimension_keys": dimension_keys,
                "residual": residual,
                "fit": fit,
                "row_count": len(rows),
                "skipped_sparse_rows": skipped_sparse,
            },
            limitations=limitations,
            result_refs=result_refs,
        )
    if skipped_sparse and not safe_rows:
        return make_evidence_envelope(
            "joint_attribution",
            evidence_type="insufficient_evidence",
            strength="insufficient",
            wording_limit="blocked",
            typed_payload={
                "dimension_keys": dimension_keys,
                "residual": residual,
                "fit": fit,
                "row_count": len(rows),
                "skipped_sparse_rows": skipped_sparse,
            },
            limitations=("sparse_cell",),
            result_refs=result_refs,
        )

    combinations, skipped = _combination_deltas(
        safe_rows,
        dimension_keys=dimension_keys,
        group_key=group_key,
        target_group=target_group,
        baseline_group=baseline_group,
        amount_key=amount_key,
    )
    if not combinations:
        return make_evidence_envelope(
            "joint_attribution",
            evidence_type="insufficient_evidence",
            strength="low",
            wording_limit="insufficient",
            typed_payload={
                "dimension_keys": dimension_keys,
                "residual": residual,
                "fit": fit,
                "skipped_rows_or_combinations": skipped,
            },
            limitations=("no_comparable_joint_combinations",),
            result_refs=result_refs,
        )

    total_delta = sum(item["delta"] for item in combinations)
    absolute_total_delta = sum(abs(item["delta"]) for item in combinations)
    for item in combinations:
        item["delta_share"] = item["delta"] / total_delta if total_delta else 0.0
        item["absolute_delta_share"] = (
            abs(item["delta"]) / absolute_total_delta if absolute_total_delta else 0.0
        )

    combinations.sort(key=lambda item: abs(item["delta"]), reverse=True)
    marginals = _marginal_contributions(
        combinations, dimension_keys, absolute_total_delta
    )
    decision = _dimension_decision(combinations, marginals)
    leading = combinations[0]
    top_3_absolute_delta_share = sum(
        item["absolute_delta_share"] for item in combinations[:3]
    )
    return make_evidence_envelope(
        "joint_attribution",
        evidence_type="accounting_contribution",
        strength="medium",
        wording_limit="candidate",
        typed_payload={
            "dimension_keys": dimension_keys,
            "residual": residual,
            "fit": fit,
            "total_delta": total_delta,
            "absolute_total_delta": absolute_total_delta,
            "combination_count": len(combinations),
            "skipped_sparse_rows": skipped_sparse,
            "leading_absolute_delta_share": leading["absolute_delta_share"],
            "top_3_absolute_delta_share": top_3_absolute_delta_share,
            "top_combinations": tuple(combinations[:5]),
            "top_lifts": tuple(item for item in combinations if item["delta"] > 0)[:5],
            "top_drags": tuple(item for item in combinations if item["delta"] < 0)[:5],
            "marginal_contributions": marginals,
            "dimension_decision": decision,
            "skipped_rows_or_combinations": skipped,
        },
        limitations=tuple(
            reason
            for reason, present in (
                ("skipped_incomplete_joint_combinations", bool(skipped)),
                ("sparse_cell", bool(skipped_sparse)),
                ("segment_bridge_not_supplied", segment_evidence is None),
            )
            if present
        ),
        result_refs=result_refs,
    )


def _infer_dimension_keys(
    rows: tuple[dict[str, Any], ...],
    group_key: str,
    amount_key: str,
) -> tuple[str, ...]:
    if not rows:
        return ()
    excluded = {group_key, amount_key, "n", "sample_size", "order_count", "user_count"}
    keys = []
    for key in rows[0]:
        if key in excluded:
            continue
        if any(isinstance(row.get(key), str) for row in rows):
            keys.append(key)
    return tuple(keys[:3])


def _combination_deltas(
    rows: tuple[dict[str, Any], ...],
    *,
    dimension_keys: tuple[str, ...],
    group_key: str,
    target_group: str,
    baseline_group: str,
    amount_key: str,
) -> tuple[list[dict[str, Any]], int]:
    pairs: dict[tuple[str, ...], dict[str, float]] = {}
    skipped = 0
    for row in rows:
        values = tuple(
            str(row.get(key))
            for key in dimension_keys
            if row.get(key) not in (None, "")
        )
        group = row.get(group_key)
        amount = _number(row.get(amount_key))
        if len(values) != len(dimension_keys) or group is None or amount is None:
            skipped += 1
            continue
        pairs.setdefault(values, {})[str(group)] = amount

    combinations = []
    for values, groups in pairs.items():
        if baseline_group not in groups or target_group not in groups:
            skipped += 1
            continue
        baseline = groups[baseline_group]
        target = groups[target_group]
        delta = target - baseline
        combinations.append(
            {
                "dimension_values": values,
                "baseline_amount": baseline,
                "target_amount": target,
                "delta": delta,
                "delta_ratio": delta / abs(baseline) if baseline else None,
            }
        )
    return combinations, skipped


def _marginal_contributions(
    combinations: list[dict[str, Any]],
    dimension_keys: tuple[str, ...],
    absolute_total_delta: float,
) -> tuple[dict[str, Any], ...]:
    marginals: list[dict[str, Any]] = []
    for index, dimension in enumerate(dimension_keys):
        totals: dict[str, float] = {}
        for item in combinations:
            value = item["dimension_values"][index]
            totals[value] = totals.get(value, 0.0) + item["delta"]
        ranked = sorted(totals.items(), key=lambda item: abs(item[1]), reverse=True)
        marginals.append(
            {
                "dimension": dimension,
                "top_values": tuple(
                    {
                        "value": value,
                        "delta": delta,
                        "absolute_delta_share": abs(delta) / absolute_total_delta
                        if absolute_total_delta
                        else 0.0,
                    }
                    for value, delta in ranked[:5]
                ),
            }
        )
    return tuple(marginals)


def _dimension_decision(
    combinations: list[dict[str, Any]],
    marginals: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    top_joint_share = combinations[0]["absolute_delta_share"] if combinations else 0.0
    best_marginal = None
    for marginal in marginals:
        top_values = marginal.get("top_values") or ()
        if not top_values:
            continue
        candidate = {"dimension": marginal["dimension"], **top_values[0]}
        if (
            best_marginal is None
            or candidate["absolute_delta_share"] > best_marginal["absolute_delta_share"]
        ):
            best_marginal = candidate

    if best_marginal and best_marginal["absolute_delta_share"] >= max(
        0.80, top_joint_share
    ):
        return {
            "action": "downgrade_to_single_dimension",
            "dimension": best_marginal["dimension"],
            "reason": "single_dimension_explains_most_joint_movement",
        }
    return {
        "action": "keep_joint",
        "reason": "joint_cells_explain_movement_better_than_any_single_dimension",
    }


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
