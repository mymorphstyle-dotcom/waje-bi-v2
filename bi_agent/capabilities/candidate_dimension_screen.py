from __future__ import annotations

from collections.abc import Iterable, Mapping
from math import isclose
from typing import Any

from bi_agent.capabilities import make_evidence_envelope


_SAMPLE_SIZE_KEYS = (
    "n",
    "sample_size",
    "order_count",
    "paid_orders",
    "orders",
    "user_count",
    "paid_users",
)
_UNKNOWN_VALUES = frozenset(("", "unknown", "null", "none", "n/a"))
_LOCALIZABLE_FACTORS = frozenset({"paid_users", "paid_frequency", "avg_order_amount"})
_MIN_DIMENSION_DIFFERENTIATION = 0.01
_MIN_EXCESS_CHANGE_RATIO = 0.05
_NEAR_CONSTANT_DOMINANT_SHARE = 0.98
_NEAR_CONSTANT_MAX_SHARE_SHIFT = 0.01


def candidate_dimension_screen(
    rows_by_dimension: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    overall_by_group: Mapping[str, Any],
    complete_dimensions: Iterable[str],
    dimension_labels: Mapping[str, str] | None = None,
    dimension_metadata: Mapping[str, Mapping[str, Any]] | None = None,
    global_primary_factor: str = "",
    group_key: str = "group",
    target_group: str = "target",
    baseline_group: str = "baseline",
    amount_key: str = "amount",
    order_key: str = "paid_orders",
    user_key: str = "paid_users",
    min_sample_size: int = 10,
    top_k: int = 5,
    reconciliation_tolerance: float = 0.01,
    result_refs: tuple[str, ...] = (),
):
    """Screen complete dimension scans as non-causal localization candidates."""

    complete = {str(dimension) for dimension in complete_dimensions}
    primary_factor = (
        str(global_primary_factor)
        if str(global_primary_factor) in _LOCALIZABLE_FACTORS
        else ""
    )
    metadata = {
        str(dimension): dict(value)
        for dimension, value in (dimension_metadata or {}).items()
        if str(dimension) and isinstance(value, Mapping)
    }
    baseline_overall = _number(overall_by_group.get(baseline_group))
    target_overall = _number(overall_by_group.get(target_group))
    overall_available = baseline_overall is not None and target_overall is not None
    profiles = tuple(
        _dimension_profile(
            str(dimension),
            tuple(rows),
            complete=str(dimension) in complete and overall_available,
            group_key=group_key,
            target_group=target_group,
            baseline_group=baseline_group,
            amount_key=amount_key,
            order_key=order_key,
            user_key=user_key,
            baseline_overall=baseline_overall,
            target_overall=target_overall,
            global_primary_factor=primary_factor,
            dimension_metadata=metadata.get(str(dimension), {}),
            min_sample_size=min_sample_size,
            top_k=top_k,
            reconciliation_tolerance=reconciliation_tolerance,
        )
        for dimension, rows in rows_by_dimension.items()
    )
    _annotate_hierarchy_depths(profiles)
    labels = {
        dimension: str(value.get("business_name") or dimension)
        for dimension, value in metadata.items()
    }
    labels.update(
        {
            str(dimension): str(label)
            for dimension, label in (dimension_labels or {}).items()
            if str(dimension) and str(label)
        }
    )
    diagnostic_priorities = tuple(
        {
            **candidate,
            "priority_rank": index,
        }
        for index, candidate in enumerate(
            sorted(
                (
                    {
                        "dimension": profile["dimension"],
                        "dimension_label": labels.get(
                            profile["dimension"], profile["dimension"]
                        ),
                        "diagnostic_priority_score": profile[
                            "diagnostic_priority_score"
                        ],
                        "global_primary_factor": primary_factor,
                        "leading_value": profile["leading_value"],
                        "leading_direction": profile["leading_direction"],
                        "leading_absolute_delta": profile["leading_absolute_delta"],
                        "leading_excess_value": profile["leading_excess_value"],
                        "leading_absolute_excess_delta": profile[
                            "leading_absolute_excess_delta"
                        ],
                        "excess_movement": profile["excess_movement"],
                        "excess_change_ratio": profile["excess_change_ratio"],
                        "dimension_differentiation_score": profile[
                            "dimension_differentiation_score"
                        ],
                        "primary_factor_alignment_coverage": profile[
                            "primary_factor_alignment_coverage"
                        ],
                        "primary_factor_alignment_score": profile[
                            "primary_factor_alignment_score"
                        ],
                        "diagnostic_priority_components": profile[
                            "diagnostic_priority_components"
                        ],
                        "hierarchy_id": profile["hierarchy_id"],
                        "hierarchy_level": profile["hierarchy_level"],
                        "parent_dimension": profile["parent_dimension"],
                        "hierarchy_depth": profile["hierarchy_depth"],
                    }
                    for profile in profiles
                    if profile["business_readout_eligible"]
                ),
                key=lambda item: (
                    item["diagnostic_priority_score"],
                    item["excess_change_ratio"],
                    item["dimension_differentiation_score"],
                    item["primary_factor_alignment_coverage"],
                    item["hierarchy_depth"],
                    item["leading_absolute_excess_delta"],
                    item["dimension"],
                ),
                reverse=True,
            ),
            start=1,
        )
    )
    eligible = tuple(item["dimension"] for item in diagnostic_priorities)
    coverage_ready = tuple(
        profile["dimension"] for profile in profiles if profile["candidate_eligible"]
    )
    selected_candidate = diagnostic_priorities[0] if diagnostic_priorities else None
    selected_profile = next(
        (
            profile
            for profile in profiles
            if selected_candidate
            and profile["dimension"] == selected_candidate["dimension"]
        ),
        None,
    )
    selected_segment = _selected_profile_segment(selected_profile)
    hierarchy_diagnostics = _build_hierarchy_diagnostics(profiles)
    selected_hierarchy_id = (
        str(selected_candidate.get("hierarchy_id") or "") if selected_candidate else ""
    )
    if selected_hierarchy_id:
        selected_hierarchy_profiles = _related_hierarchy_readout_profiles(
            profiles,
            selected_dimension=str(selected_candidate["dimension"]),
        )
    elif selected_profile is not None:
        selected_hierarchy_profiles = (selected_profile,)
    else:
        selected_hierarchy_profiles = ()
    selected_hierarchy_dimensions = tuple(
        profile["dimension"] for profile in selected_hierarchy_profiles
    )
    selected_business_readouts = tuple(
        _profile_business_readout(
            profile,
            dimension_label=labels.get(profile["dimension"], profile["dimension"]),
        )
        for profile in selected_hierarchy_profiles
        if _selected_profile_segment(profile) is not None
    )
    profiles_by_dimension = {
        str(profile["dimension"]): profile for profile in profiles
    }
    selected_readout_dimensions = {
        str(item["dimension"]) for item in selected_business_readouts
    }
    additional_business_readouts = tuple(
        _profile_business_readout(
            profiles_by_dimension[str(candidate["dimension"])],
            dimension_label=str(candidate["dimension_label"]),
        )
        for candidate in diagnostic_priorities
        if str(candidate["dimension"]) in profiles_by_dimension
        and str(candidate["dimension"]) not in selected_readout_dimensions
        and _selected_profile_segment(
            profiles_by_dimension[str(candidate["dimension"])]
        )
        is not None
    )
    business_readouts = (
        *selected_business_readouts,
        *additional_business_readouts,
    )
    limitation_values = {
        limitation for profile in profiles for limitation in profile["limitations"]
    }
    if not overall_available:
        limitation_values.add("overall_reconciliation_unavailable")
    limitations = tuple(sorted(limitation_values))
    numeric_facts = {
        "dimension_count": len(profiles),
        "eligible_dimension_count": len(eligible),
    }
    if selected_segment is not None:
        numeric_facts.update(
            {
                "paid_amount_baseline_value": selected_segment["baseline_amount"],
                "paid_amount_target_value": selected_segment["target_amount"],
                "paid_amount_delta": selected_segment["delta"],
            }
        )
        if selected_segment["baseline_amount"]:
            numeric_facts["paid_amount_relative_change"] = selected_segment[
                "delta"
            ] / abs(selected_segment["baseline_amount"])
    for readout in business_readouts:
        prefix = str(readout["dimension"])
        numeric_facts.update(
            {
                f"{prefix}_paid_amount_baseline_value": readout["baseline_amount"],
                f"{prefix}_paid_amount_target_value": readout["target_amount"],
                f"{prefix}_paid_amount_delta": readout["delta"],
                f"{prefix}_paid_amount_excess_delta": readout["excess_delta"],
            }
        )
    selected_dimension = selected_candidate["dimension"] if selected_candidate else ""
    selected_label = selected_candidate["dimension_label"] if selected_candidate else ""
    selected_value = selected_segment["value"] if selected_segment else ""
    if business_readouts:
        label_groups = [
            "→".join(
                item["dimension_label"] for item in selected_business_readouts
            )
        ] if selected_business_readouts else []
        label_groups.extend(
            item["dimension_label"]
            for item in business_readouts
            if str(item["dimension"]) not in selected_readout_dimensions
        )
        path_label = "、".join(label_groups)
        detail = "；".join(
            item["business_readout"] for item in business_readouts
        )
        business_readout = (
            f"当前有信息量的定位维度依次为{path_label}。{detail}。"
            "这些结果用于定位，跨维度不可相加。"
        )
    else:
        business_readout = (
            "当前候选维度没有形成同时通过对账、样本、超额变化和区分度门槛的定位结果。"
        )
    return make_evidence_envelope(
        "candidate_dimension_screen",
        evidence_type="statistical_association"
        if eligible
        else "insufficient_evidence",
        strength="medium" if eligible else "low",
        wording_limit="candidate" if eligible else "insufficient",
        numeric_facts=numeric_facts,
        typed_payload={
            "analysis_role": "auxiliary_localization",
            "ranking_scope": "cross_dimension_diagnostic_priority",
            "dimension_ranking_basis": (
                "excess_change_differentiation_primary_factor_alignment"
            ),
            "global_primary_factor": primary_factor,
            "causal_claim_allowed": False,
            "formula_contribution_comparable": False,
            "cross_dimension_additivity_allowed": False,
            "within_dimension_amount_contribution_additive": True,
            "excess_delta_additive_to_total_change": False,
            "excess_delta_definition": (
                "目标期实际金额减去按基线份额分配的目标期预期金额；"
                "用于识别结构性超额变化，不作为因果贡献。"
            ),
            "business_readout_gate": {
                "minimum_dimension_differentiation": _MIN_DIMENSION_DIFFERENTIATION,
                "minimum_excess_change_ratio": _MIN_EXCESS_CHANGE_RATIO,
                "near_constant_dominant_share": _NEAR_CONSTANT_DOMINANT_SHARE,
                "near_constant_max_share_shift": _NEAR_CONSTANT_MAX_SHARE_SHIFT,
            },
            "coverage_ready_dimensions": coverage_ready,
            "eligible_dimensions": eligible,
            "diagnostic_priorities": diagnostic_priorities,
            "ranked_dimension_candidates": diagnostic_priorities,
            "selected_dimension": selected_dimension,
            "selected_dimension_label": selected_label,
            "selected_value": selected_value,
            "selected_hierarchy_id": selected_hierarchy_id,
            "selected_hierarchy_dimensions": selected_hierarchy_dimensions,
            "selected_business_readouts": selected_business_readouts,
            "business_readouts": business_readouts,
            "dimension_findings": tuple(
                {
                    **readout,
                    "finding_type": "hierarchical_localization",
                    "dimension_id": readout["dimension"],
                    "member": readout["value"],
                    "evidence_state": "verified",
                }
                for readout in business_readouts
            ),
            "hierarchy_diagnostics": hierarchy_diagnostics,
            "business_readout": business_readout,
            "claim_boundary": (
                "维度内部的分群金额变化可以对账；维度之间是重叠切片，"
                "诊断优先级不可相加，也不能写成跨维度贡献排名。"
            ),
            "dimension_profiles": profiles,
        },
        limitations=limitations,
        result_refs=result_refs,
    )


def _dimension_profile(
    dimension: str,
    rows: tuple[Mapping[str, Any], ...],
    *,
    complete: bool,
    group_key: str,
    target_group: str,
    baseline_group: str,
    amount_key: str,
    order_key: str,
    user_key: str,
    baseline_overall: float | None,
    target_overall: float | None,
    global_primary_factor: str,
    dimension_metadata: Mapping[str, Any],
    min_sample_size: int,
    top_k: int,
    reconciliation_tolerance: float,
) -> dict[str, Any]:
    pairs: dict[str, dict[str, dict[str, Any]]] = {}
    invalid_rows = 0
    for row in rows:
        group = str(row.get(group_key) or "")
        amount = _number(row.get(amount_key))
        if group not in {baseline_group, target_group} or amount is None:
            invalid_rows += 1
            continue
        value = _dimension_value(row.get(dimension))
        cell = pairs.setdefault(value, {}).setdefault(
            group,
            {
                "amount": 0.0,
                "orders": 0.0,
                "users": 0.0,
                "orders_observed": False,
                "users_observed": False,
                "samples": [],
            },
        )
        cell["amount"] += amount
        orders = _metric_number(row, order_key, "paid_orders", "orders")
        if orders is not None:
            cell["orders"] += orders
            cell["orders_observed"] = True
        users = _metric_number(row, user_key, "paid_users")
        if users is not None:
            cell["users"] += users
            cell["users_observed"] = True
        sample_size = _sample_size(row)
        if sample_size is not None:
            cell["samples"].append(sample_size)

    contributions: list[dict[str, Any]] = []
    incomplete_values = 0
    for value, groups in pairs.items():
        baseline_present = baseline_group in groups
        target_present = target_group in groups
        if not complete and (not baseline_present or not target_present):
            incomplete_values += 1
            continue
        empty_cell = {
            "amount": 0.0,
            "orders": 0.0,
            "users": 0.0,
            "orders_observed": False,
            "users_observed": False,
            "samples": [],
        }
        baseline_cell = groups.get(baseline_group, empty_cell)
        target_cell = groups.get(target_group, empty_cell)
        baseline_amount = float(baseline_cell["amount"])
        target_amount = float(target_cell["amount"])
        baseline_orders = (
            float(baseline_cell["orders"]) if baseline_cell["orders_observed"] else None
        )
        target_orders = (
            float(target_cell["orders"]) if target_cell["orders_observed"] else None
        )
        baseline_users = (
            float(baseline_cell["users"]) if baseline_cell["users_observed"] else None
        )
        target_users = (
            float(target_cell["users"]) if target_cell["users_observed"] else None
        )
        baseline_frequency = _safe_divide(baseline_orders, baseline_users)
        target_frequency = _safe_divide(target_orders, target_users)
        baseline_average = _safe_divide(baseline_amount, baseline_orders)
        target_average = _safe_divide(target_amount, target_orders)
        observed_samples = tuple(
            sample for cell in groups.values() for sample in cell["samples"]
        )
        sample_verified = bool(observed_samples)
        sample_eligible = sample_verified and min(observed_samples) >= min_sample_size
        movement_type = "existing"
        if target_present and not baseline_present:
            movement_type = "entrant"
        elif baseline_present and not target_present:
            movement_type = "exit"
        contributions.append(
            {
                "value": value,
                "is_unknown": value == "Unknown",
                "baseline_amount": baseline_amount,
                "target_amount": target_amount,
                "delta": target_amount - baseline_amount,
                "baseline_paid_orders": baseline_orders,
                "target_paid_orders": target_orders,
                "baseline_paid_users": baseline_users,
                "target_paid_users": target_users,
                "baseline_paid_frequency": baseline_frequency,
                "target_paid_frequency": target_frequency,
                "baseline_avg_order_amount": baseline_average,
                "target_avg_order_amount": target_average,
                "factor_changes": {
                    "paid_users": _factor_change(baseline_users, target_users),
                    "paid_frequency": _factor_change(
                        baseline_frequency,
                        target_frequency,
                    ),
                    "avg_order_amount": _factor_change(
                        baseline_average,
                        target_average,
                    ),
                },
                "amount_contribution_scope": "within_dimension",
                "movement_type": movement_type,
                "sample_size_verified": sample_verified,
                "sample_eligible": sample_eligible,
            }
        )

    observed_baseline = sum(item["baseline_amount"] for item in contributions)
    observed_target = sum(item["target_amount"] for item in contributions)
    reconciled = (
        complete
        and baseline_overall is not None
        and target_overall is not None
        and _reconciles(
            observed_baseline,
            baseline_overall,
            reconciliation_tolerance,
        )
        and _reconciles(observed_target, target_overall, reconciliation_tolerance)
    )
    total_delta = observed_target - observed_baseline
    for item in contributions:
        baseline_share = _safe_divide(
            item["baseline_amount"],
            observed_baseline,
        )
        target_share = _safe_divide(
            item["target_amount"],
            observed_target,
        )
        expected_target_at_baseline_mix = (
            baseline_share * observed_target if baseline_share is not None else None
        )
        excess_delta = (
            item["target_amount"] - expected_target_at_baseline_mix
            if expected_target_at_baseline_mix is not None
            else None
        )
        item["baseline_amount_share"] = baseline_share
        item["target_amount_share"] = target_share
        item["expected_target_amount_at_baseline_mix"] = expected_target_at_baseline_mix
        item["expected_delta_at_baseline_mix"] = (
            expected_target_at_baseline_mix - item["baseline_amount"]
            if expected_target_at_baseline_mix is not None
            else None
        )
        item["excess_delta"] = excess_delta
        item["amount_contribution_share"] = (
            item["delta"] / total_delta if total_delta else None
        )
    ranked = [item for item in contributions if item["sample_eligible"]]
    has_movement = any(
        not isclose(item["delta"], 0.0, abs_tol=reconciliation_tolerance)
        for item in ranked
    )
    candidate_eligible = reconciled and bool(ranked) and has_movement
    publishable_ranked = ranked if candidate_eligible else ()
    top_lifts = tuple(
        sorted(
            (item for item in publishable_ranked if item["delta"] > 0),
            key=lambda item: item["delta"],
            reverse=True,
        )[:top_k]
    )
    top_drags = tuple(
        sorted(
            (item for item in publishable_ranked if item["delta"] < 0),
            key=lambda item: item["delta"],
        )[:top_k]
    )
    unknown = next(
        (item for item in contributions if item["is_unknown"]),
        {"baseline_amount": 0.0, "target_amount": 0.0, "delta": 0.0},
    )
    unknown_bucket = {
        "baseline_amount": unknown["baseline_amount"],
        "target_amount": unknown["target_amount"],
        "delta": unknown["delta"],
    }
    ranked_by_movement = sorted(
        ranked,
        key=lambda item: abs(item["delta"]),
        reverse=True,
    )
    leading = ranked_by_movement[0] if ranked_by_movement else None
    absolute_movement = sum(abs(item["delta"]) for item in ranked)
    all_primary_factor_segments = tuple(
        sorted(
            (
                item
                for item in ranked
                if _factor_aligns_with_amount(item, global_primary_factor)
            ),
            key=lambda item: abs(item["delta"]),
            reverse=True,
        )
    )
    primary_factor_segments = all_primary_factor_segments[:top_k]
    aligned_movement = sum(abs(item["delta"]) for item in all_primary_factor_segments)
    alignment_coverage = (
        aligned_movement / absolute_movement if absolute_movement else 0.0
    )
    leading_concentration = (
        abs(leading["delta"]) / absolute_movement
        if leading is not None and absolute_movement
        else 0.0
    )
    excess_ranked = tuple(item for item in ranked if item["excess_delta"] is not None)
    top_excess_lifts = tuple(
        sorted(
            (item for item in excess_ranked if item["excess_delta"] > 0),
            key=lambda item: item["excess_delta"],
            reverse=True,
        )[:top_k]
    )
    top_excess_drags = tuple(
        sorted(
            (item for item in excess_ranked if item["excess_delta"] < 0),
            key=lambda item: item["excess_delta"],
        )[:top_k]
    )
    excess_movement = (
        sum(abs(float(item["excess_delta"])) for item in excess_ranked) / 2.0
    )
    dimension_differentiation_score = (
        sum(
            abs(
                float(item["target_amount_share"])
                - float(item["baseline_amount_share"])
            )
            for item in excess_ranked
            if item["target_amount_share"] is not None
            and item["baseline_amount_share"] is not None
        )
        / 2.0
    )
    movement_reference = max(
        abs(total_delta),
        abs(observed_baseline) * reconciliation_tolerance,
        abs(observed_target) * reconciliation_tolerance,
        1e-12,
    )
    excess_change_ratio = min(1.0, excess_movement / movement_reference)
    dominant_baseline_share = max(
        (
            float(item["baseline_amount_share"])
            for item in excess_ranked
            if item["baseline_amount_share"] is not None
        ),
        default=0.0,
    )
    dominant_target_share = max(
        (
            float(item["target_amount_share"])
            for item in excess_ranked
            if item["target_amount_share"] is not None
        ),
        default=0.0,
    )
    near_constant_dimension = (
        bool(excess_ranked)
        and dominant_baseline_share >= _NEAR_CONSTANT_DOMINANT_SHARE
        and dominant_target_share >= _NEAR_CONSTANT_DOMINANT_SHARE
        and dimension_differentiation_score <= _NEAR_CONSTANT_MAX_SHARE_SHIFT
    )
    alignment_score = alignment_coverage if global_primary_factor else 1.0
    diagnostic_priority_score = (
        0.45 * excess_change_ratio
        + 0.35 * dimension_differentiation_score
        + 0.20 * alignment_score
    )
    business_readout_eligible = (
        candidate_eligible
        and not near_constant_dimension
        and dimension_differentiation_score >= _MIN_DIMENSION_DIFFERENTIATION
        and excess_change_ratio >= _MIN_EXCESS_CHANGE_RATIO
    )
    leading_excess = max(
        excess_ranked,
        key=lambda item: abs(float(item["excess_delta"])),
        default=None,
    )
    displayed_delta = sum(item["delta"] for item in (*top_lifts, *top_drags))
    limitations = []
    if not complete:
        limitations.append(f"incomplete_dimension_window:{dimension}")
    if incomplete_values:
        limitations.append(f"unpaired_dimension_values:{dimension}")
    if invalid_rows:
        limitations.append(f"invalid_dimension_rows:{dimension}")
    if complete and not reconciled:
        limitations.append(f"dimension_reconciliation_failed:{dimension}")
    if any(not item["sample_size_verified"] for item in contributions):
        limitations.append(f"sample_size_unverified:{dimension}")
    if any(
        item["sample_size_verified"] and not item["sample_eligible"]
        for item in contributions
    ):
        limitations.append(f"sparse_dimension_values:{dimension}")
    if reconciled and ranked and not has_movement:
        limitations.append(f"no_dimension_movement:{dimension}")
    if candidate_eligible and near_constant_dimension:
        limitations.append(f"near_constant_dimension:{dimension}")
    if candidate_eligible and not business_readout_eligible:
        limitations.append(f"low_dimension_information_value:{dimension}")
    if global_primary_factor and candidate_eligible and not all_primary_factor_segments:
        limitations.append(f"primary_factor_not_localized:{dimension}")
    profile_status = (
        "unavailable"
        if not complete or not rows
        else "coverage_only"
        if candidate_eligible and not business_readout_eligible
        else "ready"
        if business_readout_eligible
        and (not global_primary_factor or all_primary_factor_segments)
        else "degraded"
    )
    return {
        "dimension": dimension,
        "profile_status": profile_status,
        "window_complete": complete,
        "candidate_eligible": candidate_eligible,
        "business_readout_eligible": business_readout_eligible,
        "selection_status": (
            "business_readout_candidate"
            if business_readout_eligible
            else "internal_coverage_only"
            if candidate_eligible
            else "unavailable"
        ),
        "reconciliation_status": (
            "not_checked" if not complete else "passed" if reconciled else "failed"
        ),
        "overall_baseline_amount": baseline_overall,
        "overall_target_amount": target_overall,
        "observed_baseline_amount": observed_baseline,
        "observed_target_amount": observed_target,
        "baseline_reconciliation_gap": (
            observed_baseline - baseline_overall
            if baseline_overall is not None
            else None
        ),
        "target_reconciliation_gap": (
            observed_target - target_overall if target_overall is not None else None
        ),
        "total_delta": total_delta,
        "absolute_movement": absolute_movement,
        "leading_value": leading["value"] if leading else "",
        "leading_direction": (
            "lift"
            if leading and leading["delta"] > 0
            else "drag"
            if leading and leading["delta"] < 0
            else "flat"
        ),
        "leading_absolute_delta": abs(leading["delta"]) if leading else 0.0,
        "leading_movement_concentration": leading_concentration,
        "leading_excess_value": leading_excess["value"] if leading_excess else "",
        "leading_excess_direction": (
            "lift"
            if leading_excess and leading_excess["excess_delta"] > 0
            else "drag"
            if leading_excess and leading_excess["excess_delta"] < 0
            else "flat"
        ),
        "leading_absolute_excess_delta": (
            abs(float(leading_excess["excess_delta"])) if leading_excess else 0.0
        ),
        "top_excess_lifts": top_excess_lifts,
        "top_excess_drags": top_excess_drags,
        "excess_movement": excess_movement,
        "excess_change_ratio": excess_change_ratio,
        "dimension_differentiation_score": dimension_differentiation_score,
        "dominant_baseline_share": dominant_baseline_share,
        "dominant_target_share": dominant_target_share,
        "near_constant_dimension": near_constant_dimension,
        "global_primary_factor": global_primary_factor,
        "primary_factor_segments": primary_factor_segments,
        "primary_factor_alignment_coverage": alignment_coverage,
        "primary_factor_alignment_score": alignment_score,
        "diagnostic_priority_score": diagnostic_priority_score,
        "diagnostic_priority_components": {
            "excess_change_ratio": excess_change_ratio,
            "dimension_differentiation_score": (dimension_differentiation_score),
            "primary_factor_alignment_score": alignment_score,
        },
        "hierarchy_id": str(dimension_metadata.get("hierarchy_id") or ""),
        "hierarchy_level": str(dimension_metadata.get("hierarchy_level") or ""),
        "parent_dimension": str(dimension_metadata.get("parent_dimension") or ""),
        "hierarchy_depth": 0,
        "unknown_bucket": unknown_bucket,
        "top_lifts": top_lifts,
        "top_drags": top_drags,
        "displayed_delta": displayed_delta,
        "remainder_delta": total_delta - displayed_delta,
        "segment_count": len(contributions),
        "unpaired_dimension_value_count": incomplete_values,
        "suppressed_segment_count": len(contributions) - len(ranked),
        "limitations": tuple(limitations),
    }


def _annotate_hierarchy_depths(
    profiles: tuple[dict[str, Any], ...],
) -> None:
    by_dimension = {
        str(profile.get("dimension") or ""): profile
        for profile in profiles
        if str(profile.get("dimension") or "")
    }

    def resolve_depth(
        profile: Mapping[str, Any],
        trail: frozenset[str] = frozenset(),
    ) -> int:
        dimension = str(profile.get("dimension") or "")
        parent_dimension = str(profile.get("parent_dimension") or "")
        hierarchy_id = str(profile.get("hierarchy_id") or "")
        if not parent_dimension or dimension in trail:
            return 0
        parent = by_dimension.get(parent_dimension)
        if parent is None or str(parent.get("hierarchy_id") or "") != hierarchy_id:
            return 0
        return 1 + resolve_depth(parent, trail | {dimension})

    for profile in profiles:
        profile["hierarchy_depth"] = resolve_depth(profile)


def _build_hierarchy_diagnostics(
    profiles: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    hierarchy_ids = tuple(
        dict.fromkeys(
            str(profile.get("hierarchy_id") or "")
            for profile in profiles
            if str(profile.get("hierarchy_id") or "")
        )
    )
    diagnostics = []
    for hierarchy_id in hierarchy_ids:
        levels = tuple(
            sorted(
                (
                    profile
                    for profile in profiles
                    if profile["hierarchy_id"] == hierarchy_id
                ),
                key=lambda profile: (
                    profile["hierarchy_depth"],
                    profile["dimension"],
                ),
            )
        )
        dimension_paths = _hierarchy_dimension_paths(levels)
        diagnostics.append(
            {
                "hierarchy_id": hierarchy_id,
                "dimension_path": (
                    dimension_paths[0] if len(dimension_paths) == 1 else ()
                ),
                "dimension_paths": dimension_paths,
                "business_readout_paths": tuple(
                    tuple(
                        dimension
                        for dimension in path
                        if next(
                            profile
                            for profile in levels
                            if profile["dimension"] == dimension
                        )["business_readout_eligible"]
                    )
                    for path in dimension_paths
                    if any(
                        next(
                            profile
                            for profile in levels
                            if profile["dimension"] == dimension
                        )["business_readout_eligible"]
                        for dimension in path
                    )
                ),
                "coverage_dimensions": tuple(
                    profile["dimension"]
                    for profile in levels
                    if profile["selection_status"] == "internal_coverage_only"
                ),
                "business_readout_dimensions": tuple(
                    profile["dimension"]
                    for profile in levels
                    if profile["business_readout_eligible"]
                ),
                "levels": tuple(
                    {
                        "dimension": profile["dimension"],
                        "hierarchy_level": profile["hierarchy_level"],
                        "parent_dimension": profile["parent_dimension"],
                        "hierarchy_depth": profile["hierarchy_depth"],
                        "profile_status": profile["profile_status"],
                        "selection_status": profile["selection_status"],
                        "reconciliation_status": profile["reconciliation_status"],
                        "dimension_differentiation_score": profile[
                            "dimension_differentiation_score"
                        ],
                        "excess_movement": profile["excess_movement"],
                        "excess_change_ratio": profile["excess_change_ratio"],
                        "primary_factor_alignment_score": profile[
                            "primary_factor_alignment_score"
                        ],
                        "diagnostic_priority_score": profile[
                            "diagnostic_priority_score"
                        ],
                    }
                    for profile in levels
                ),
            }
        )
    return tuple(diagnostics)


def _hierarchy_dimension_paths(
    profiles: tuple[Mapping[str, Any], ...],
) -> tuple[tuple[str, ...], ...]:
    by_dimension = {
        str(profile.get("dimension") or ""): profile
        for profile in profiles
        if str(profile.get("dimension") or "")
    }
    children: dict[str, list[str]] = {}
    roots = []
    for dimension, profile in by_dimension.items():
        parent = str(profile.get("parent_dimension") or "")
        if parent and parent in by_dimension:
            children.setdefault(parent, []).append(dimension)
        else:
            roots.append(dimension)
    paths: list[tuple[str, ...]] = []

    def visit(
        dimension: str,
        path: tuple[str, ...],
    ) -> None:
        if dimension in path:
            paths.append((*path, dimension))
            return
        next_path = (*path, dimension)
        descendants = tuple(sorted(children.get(dimension, ())))
        if not descendants:
            paths.append(next_path)
            return
        for child in descendants:
            visit(child, next_path)

    for root in sorted(roots):
        visit(root, ())
    covered = {dimension for path in paths for dimension in path}
    for dimension in sorted(set(by_dimension) - covered):
        visit(dimension, ())
    return tuple(paths)


def _related_hierarchy_readout_profiles(
    profiles: tuple[dict[str, Any], ...],
    *,
    selected_dimension: str,
) -> tuple[dict[str, Any], ...]:
    by_dimension = {
        str(profile.get("dimension") or ""): profile
        for profile in profiles
        if str(profile.get("dimension") or "")
    }
    selected = by_dimension.get(selected_dimension)
    if selected is None:
        return ()
    hierarchy_id = str(selected.get("hierarchy_id") or "")

    def ancestor_dimensions(dimension: str) -> frozenset[str]:
        ancestors: set[str] = set()
        current = by_dimension.get(dimension)
        while current is not None:
            parent = str(current.get("parent_dimension") or "")
            if not parent or parent in ancestors:
                break
            parent_profile = by_dimension.get(parent)
            if (
                parent_profile is None
                or str(parent_profile.get("hierarchy_id") or "") != hierarchy_id
            ):
                break
            ancestors.add(parent)
            current = parent_profile
        return frozenset(ancestors)

    selected_ancestors = ancestor_dimensions(selected_dimension)
    related = tuple(
        profile
        for profile in profiles
        if profile["hierarchy_id"] == hierarchy_id
        and profile["business_readout_eligible"]
        and (
            profile["dimension"] == selected_dimension
            or profile["dimension"] in selected_ancestors
            or selected_dimension in ancestor_dimensions(profile["dimension"])
        )
    )
    return tuple(
        sorted(
            related,
            key=lambda profile: (
                profile["hierarchy_depth"],
                profile["dimension"],
            ),
        )
    )


def _selected_profile_segment(
    profile: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if profile is None:
        return None
    primary_factor_segments = tuple(profile.get("primary_factor_segments") or ())
    excess_segments = tuple(
        (
            *(profile.get("top_excess_lifts") or ()),
            *(profile.get("top_excess_drags") or ()),
        )
    )
    candidates = primary_factor_segments or excess_segments
    return max(
        candidates,
        key=lambda item: abs(float(item.get("excess_delta") or 0.0)),
        default=None,
    )


def _profile_business_readout(
    profile: Mapping[str, Any],
    *,
    dimension_label: str,
) -> dict[str, Any]:
    segment = _selected_profile_segment(profile)
    if segment is None:
        return {}
    excess_delta = float(segment.get("excess_delta") or 0.0)
    value = str(segment.get("value") or "")
    return {
        "dimension": str(profile.get("dimension") or ""),
        "dimension_label": dimension_label,
        "hierarchy_id": str(profile.get("hierarchy_id") or ""),
        "hierarchy_level": str(profile.get("hierarchy_level") or ""),
        "parent_dimension": str(profile.get("parent_dimension") or ""),
        "value": value,
        "baseline_amount": float(segment["baseline_amount"]),
        "target_amount": float(segment["target_amount"]),
        "delta": float(segment["delta"]),
        "excess_delta": excess_delta,
        "dimension_differentiation_score": float(
            profile.get("dimension_differentiation_score") or 0.0
        ),
        "primary_factor_alignment_coverage": float(
            profile.get("primary_factor_alignment_coverage") or 0.0
        ),
        "business_readout": (
            f"{dimension_label}重点关注{value}：目标期付费金额"
            f"{float(segment['target_amount']):,.2f}，基线期"
            f"{float(segment['baseline_amount']):,.2f}，实际变化"
            f"{float(segment['delta']):+,.2f}，相对基线结构的超额变化"
            f"{excess_delta:+,.2f}"
        ),
    }


def _dimension_value(value: Any) -> str:
    if value is None:
        return "Unknown"
    normalized = str(value).strip()
    return "Unknown" if normalized.casefold() in _UNKNOWN_VALUES else normalized


def _sample_size(row: Mapping[str, Any]) -> int | None:
    for key in _SAMPLE_SIZE_KEYS:
        if row.get(key) is None:
            continue
        try:
            return int(row[key])
        except (TypeError, ValueError):
            return None
    return None


def _metric_number(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in dict.fromkeys(str(item) for item in keys if str(item)):
        value = _number(row.get(key))
        if value is not None:
            return value
    return None


def _safe_divide(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0.0):
        return None
    return numerator / denominator


def _factor_change(
    baseline_value: float | None,
    target_value: float | None,
) -> dict[str, Any]:
    if baseline_value is None or target_value is None:
        return {
            "status": "unavailable",
            "baseline_value": baseline_value,
            "target_value": target_value,
            "delta": None,
            "delta_ratio": None,
        }
    delta = target_value - baseline_value
    return {
        "status": "observed",
        "baseline_value": baseline_value,
        "target_value": target_value,
        "delta": delta,
        "delta_ratio": delta / abs(baseline_value) if baseline_value else None,
    }


def _factor_aligns_with_amount(
    segment: Mapping[str, Any],
    factor: str,
) -> bool:
    if not factor:
        return False
    change = (segment.get("factor_changes") or {}).get(factor) or {}
    factor_delta = _number(change.get("delta"))
    amount_delta = _number(segment.get("delta"))
    return (
        factor_delta is not None
        and amount_delta is not None
        and not isclose(factor_delta, 0.0, abs_tol=1e-12)
        and not isclose(amount_delta, 0.0, abs_tol=1e-12)
        and factor_delta * amount_delta > 0
    )


def _reconciles(observed: float, expected: float, tolerance: float) -> bool:
    return isclose(observed, expected, rel_tol=tolerance, abs_tol=tolerance)


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
