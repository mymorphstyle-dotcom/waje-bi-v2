from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from bi_agent.capabilities import make_evidence_envelope


class SegmentDistributionError(ValueError):
    pass


_MATERIAL_RECORD_LIMIT = 5


def segment_breakdown(
    rows_by_dimension: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    metric_id: str,
    group_key: str = "window_role",
    target_group: str = "target",
    baseline_group: str = "baseline",
    dimension_paths: Mapping[str, Sequence[str]] | None = None,
    result_refs: tuple[str, ...] = (),
):
    distributions = _dimension_distributions(
        rows_by_dimension,
        metric_id=metric_id,
        group_key=group_key,
        target_group=target_group,
        baseline_group=baseline_group,
        dimension_paths=dimension_paths,
    )
    findings = tuple(_breakdown_material_summary(item) for item in distributions)
    return make_evidence_envelope(
        "segment_breakdown",
        evidence_type="accounting_contribution",
        strength="quantified_contribution",
        wording_limit="quantified_within_dimension",
        numeric_facts={"dimension_count": len(distributions)},
        typed_payload={
            "metric_id": metric_id,
            "dimension_breakdowns": distributions,
            "dimension_findings": findings,
            "within_dimension_additivity_allowed": True,
            "cross_dimension_additivity_allowed": False,
            "causal_claim_allowed": False,
        },
        limitations=(),
        result_refs=result_refs,
    )


def segment_shift_compare(
    rows_by_dimension: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    metric_id: str,
    group_key: str = "window_role",
    target_group: str = "target",
    baseline_group: str = "baseline",
    dimension_paths: Mapping[str, Sequence[str]] | None = None,
    result_refs: tuple[str, ...] = (),
):
    distributions = _dimension_distributions(
        rows_by_dimension,
        metric_id=metric_id,
        group_key=group_key,
        target_group=target_group,
        baseline_group=baseline_group,
        dimension_paths=dimension_paths,
    )
    shifts = tuple(
        {
            "dimension_id": item["dimension_id"],
            "dimension_path": item["dimension_path"],
            "baseline_total": item["baseline_total"],
            "target_total": item["target_total"],
            "members": tuple(
                sorted(
                    (
                        {
                            **member,
                            "share_delta": (
                                member["target_share"] - member["baseline_share"]
                            ),
                        }
                        for member in item["members"]
                    ),
                    key=lambda member: (
                        abs(member["share_delta"]),
                        member["member"],
                    ),
                    reverse=True,
                )
            ),
        }
        for item in distributions
    )
    findings = tuple(_shift_material_summary(item) for item in shifts)
    return make_evidence_envelope(
        "segment_shift_compare",
        evidence_type="accounting_contribution",
        strength="quantified_contribution",
        wording_limit="quantified_within_dimension",
        numeric_facts={"dimension_count": len(shifts)},
        typed_payload={
            "metric_id": metric_id,
            "dimension_shifts": shifts,
            "dimension_findings": findings,
            "within_dimension_additivity_allowed": True,
            "cross_dimension_additivity_allowed": False,
            "causal_claim_allowed": False,
        },
        limitations=(),
        result_refs=result_refs,
    )


def _breakdown_material_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    total_delta = float(item["target_total"]) - float(item["baseline_total"])
    movements = tuple(
        {
            **member,
            "delta": float(member["target_value"]) - float(member["baseline_value"]),
            **(
                {
                    "delta_share": (
                        float(member["target_value"]) - float(member["baseline_value"])
                    )
                    / total_delta
                }
                if total_delta
                else {}
            ),
        }
        for member in item["members"]
    )
    top_lifts = tuple(
        sorted(
            (member for member in movements if member["delta"] > 0),
            key=lambda member: (member["delta"], member["member"]),
            reverse=True,
        )[:_MATERIAL_RECORD_LIMIT]
    )
    top_drags = tuple(
        sorted(
            (member for member in movements if member["delta"] < 0),
            key=lambda member: (member["delta"], member["member"]),
        )[:_MATERIAL_RECORD_LIMIT]
    )
    displayed_delta = sum(float(member["delta"]) for member in (*top_lifts, *top_drags))
    positive_delta_total = sum(
        float(member["delta"]) for member in movements if member["delta"] > 0
    )
    negative_delta_total = sum(
        float(member["delta"]) for member in movements if member["delta"] < 0
    )
    displayed_positive_delta = sum(float(member["delta"]) for member in top_lifts)
    displayed_negative_delta = sum(float(member["delta"]) for member in top_drags)
    displayed_member_count = len(top_lifts) + len(top_drags)
    return {
        "projection_kind": "claim_material_summary",
        "finding_type": "segment_breakdown",
        "evidence_state": "verified",
        "dimension_id": item["dimension_id"],
        "dimension_path": item["dimension_path"],
        "baseline_total": item["baseline_total"],
        "target_total": item["target_total"],
        "total_delta": total_delta,
        "member_count": len(movements),
        "displayed_member_count": displayed_member_count,
        "omitted_member_count": len(movements) - displayed_member_count,
        "ranking_basis": "signed_member_delta",
        "record_limit_per_direction": _MATERIAL_RECORD_LIMIT,
        "top_lifts": top_lifts,
        "top_drags": top_drags,
        "displayed_delta": displayed_delta,
        "remainder_delta": total_delta - displayed_delta,
        "positive_delta_total": positive_delta_total,
        "negative_delta_total": negative_delta_total,
        "displayed_positive_delta": displayed_positive_delta,
        "displayed_negative_delta": displayed_negative_delta,
        "remainder_positive_delta": (positive_delta_total - displayed_positive_delta),
        "remainder_negative_delta": (negative_delta_total - displayed_negative_delta),
        "total_absolute_movement": positive_delta_total - negative_delta_total,
        "displayed_absolute_movement": (
            displayed_positive_delta - displayed_negative_delta
        ),
        "remainder_absolute_movement": (
            positive_delta_total
            - displayed_positive_delta
            - negative_delta_total
            + displayed_negative_delta
        ),
        "reconciliation_status": "passed",
    }


def _shift_material_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    members = tuple(item["members"])
    top_share_lifts = tuple(
        sorted(
            (member for member in members if member["share_delta"] > 0),
            key=lambda member: (member["share_delta"], member["member"]),
            reverse=True,
        )[:_MATERIAL_RECORD_LIMIT]
    )
    top_share_drags = tuple(
        sorted(
            (member for member in members if member["share_delta"] < 0),
            key=lambda member: (member["share_delta"], member["member"]),
        )[:_MATERIAL_RECORD_LIMIT]
    )
    total_absolute_share_shift = (
        sum(abs(float(member["share_delta"])) for member in members) / 2.0
    )
    displayed_absolute_share_shift = (
        sum(
            abs(float(member["share_delta"]))
            for member in (*top_share_lifts, *top_share_drags)
        )
        / 2.0
    )
    positive_share_shift_total = sum(
        float(member["share_delta"]) for member in members if member["share_delta"] > 0
    )
    negative_share_shift_total = sum(
        float(member["share_delta"]) for member in members if member["share_delta"] < 0
    )
    displayed_positive_share_shift = sum(
        float(member["share_delta"]) for member in top_share_lifts
    )
    displayed_negative_share_shift = sum(
        float(member["share_delta"]) for member in top_share_drags
    )
    displayed_member_count = len(top_share_lifts) + len(top_share_drags)
    return {
        "projection_kind": "claim_material_summary",
        "finding_type": "segment_mix_shift",
        "evidence_state": "verified",
        "dimension_id": item["dimension_id"],
        "dimension_path": item["dimension_path"],
        "baseline_total": item["baseline_total"],
        "target_total": item["target_total"],
        "total_delta": float(item["target_total"]) - float(item["baseline_total"]),
        "member_count": len(members),
        "displayed_member_count": displayed_member_count,
        "omitted_member_count": len(members) - displayed_member_count,
        "ranking_basis": "signed_share_delta",
        "record_limit_per_direction": _MATERIAL_RECORD_LIMIT,
        "top_share_lifts": top_share_lifts,
        "top_share_drags": top_share_drags,
        "total_absolute_share_shift": total_absolute_share_shift,
        "displayed_absolute_share_shift": displayed_absolute_share_shift,
        "remainder_absolute_share_shift": (
            total_absolute_share_shift - displayed_absolute_share_shift
        ),
        "positive_share_shift_total": positive_share_shift_total,
        "negative_share_shift_total": negative_share_shift_total,
        "displayed_positive_share_shift": displayed_positive_share_shift,
        "displayed_negative_share_shift": displayed_negative_share_shift,
        "remainder_positive_share_shift": (
            positive_share_shift_total - displayed_positive_share_shift
        ),
        "remainder_negative_share_shift": (
            negative_share_shift_total - displayed_negative_share_shift
        ),
        "reconciliation_status": "passed",
    }


def _dimension_distributions(
    rows_by_dimension: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    metric_id: str,
    group_key: str,
    target_group: str,
    baseline_group: str,
    dimension_paths: Mapping[str, Sequence[str]] | None,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(rows_by_dimension, Mapping) or not rows_by_dimension:
        raise SegmentDistributionError("segment_distribution_dimensions_missing")
    metric_id = _required_name(metric_id, "segment_distribution_metric_id_invalid")
    group_key = _required_name(group_key, "segment_distribution_group_key_invalid")
    target_group = _required_name(
        target_group, "segment_distribution_target_group_invalid"
    )
    baseline_group = _required_name(
        baseline_group, "segment_distribution_baseline_group_invalid"
    )
    if target_group == baseline_group:
        raise SegmentDistributionError("segment_distribution_groups_collide")
    paths = dimension_paths or {}
    distributions = []
    for raw_dimension, raw_rows in sorted(rows_by_dimension.items()):
        dimension = _required_name(
            raw_dimension, "segment_distribution_dimension_id_invalid"
        )
        by_group: dict[str, dict[str, Decimal]] = {
            target_group: {},
            baseline_group: {},
        }
        row_count = 0
        for row in raw_rows:
            if not isinstance(row, Mapping):
                raise SegmentDistributionError(
                    f"segment_distribution_row_invalid:{dimension}"
                )
            member = _required_name(
                row.get(dimension),
                f"segment_distribution_member_missing:{dimension}",
            )
            group = _required_name(
                row.get(group_key),
                f"segment_distribution_group_missing:{dimension}",
            )
            if group not in by_group:
                raise SegmentDistributionError(
                    f"segment_distribution_group_unexpected:{dimension}:{group}"
                )
            if metric_id not in row or row.get(metric_id) is None:
                raise SegmentDistributionError(
                    f"segment_distribution_value_missing:{dimension}"
                )
            value = _decimal(row.get(metric_id), dimension=dimension)
            by_group[group][member] = by_group[group].get(member, Decimal(0)) + value
            row_count += 1
        if not row_count:
            raise SegmentDistributionError(
                f"segment_distribution_rows_empty:{dimension}"
            )
        for required_group in (baseline_group, target_group):
            if not by_group[required_group]:
                raise SegmentDistributionError(
                    f"segment_distribution_group_missing:{dimension}:{required_group}"
                )
        baseline_total = sum(by_group[baseline_group].values(), Decimal(0))
        target_total = sum(by_group[target_group].values(), Decimal(0))
        if baseline_total == 0:
            raise SegmentDistributionError(
                f"segment_distribution_total_zero:{dimension}:{baseline_group}"
            )
        if target_total == 0:
            raise SegmentDistributionError(
                f"segment_distribution_total_zero:{dimension}:{target_group}"
            )
        members = tuple(
            {
                "member": member,
                "baseline_value": float(
                    by_group[baseline_group].get(member, Decimal(0))
                ),
                "target_value": float(by_group[target_group].get(member, Decimal(0))),
                "baseline_share": float(
                    by_group[baseline_group].get(member, Decimal(0)) / baseline_total
                ),
                "target_share": float(
                    by_group[target_group].get(member, Decimal(0)) / target_total
                ),
            }
            for member in sorted(
                set(by_group[baseline_group]) | set(by_group[target_group])
            )
        )
        raw_path = paths.get(dimension, (dimension,))
        if isinstance(raw_path, (str, bytes)) or not isinstance(raw_path, Sequence):
            raise SegmentDistributionError(
                f"segment_distribution_dimension_path_invalid:{dimension}"
            )
        dimension_path = tuple(
            _required_name(
                item,
                f"segment_distribution_dimension_path_invalid:{dimension}",
            )
            for item in raw_path
        )
        if not dimension_path or dimension_path[-1] != dimension:
            raise SegmentDistributionError(
                f"segment_distribution_dimension_path_invalid:{dimension}"
            )
        distributions.append(
            {
                "dimension_id": dimension,
                "dimension_path": dimension_path,
                "baseline_total": float(baseline_total),
                "target_total": float(target_total),
                "members": members,
                "within_dimension_reconciled": True,
            }
        )
    return tuple(distributions)


def _required_name(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SegmentDistributionError(error)
    return value


def _decimal(value: Any, *, dimension: str) -> Decimal:
    if isinstance(value, bool):
        raise SegmentDistributionError(
            f"segment_distribution_value_invalid:{dimension}"
        )
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SegmentDistributionError(
            f"segment_distribution_value_invalid:{dimension}"
        ) from exc
    if not normalized.is_finite():
        raise SegmentDistributionError(
            f"segment_distribution_value_invalid:{dimension}"
        )
    return normalized


__all__ = (
    "SegmentDistributionError",
    "segment_breakdown",
    "segment_shift_compare",
)
