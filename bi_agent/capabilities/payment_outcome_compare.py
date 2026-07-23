from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from bi_agent.capabilities import make_evidence_envelope


class PaymentOutcomeCompareError(ValueError):
    pass


def payment_outcome_compare(
    rows_by_dimension: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    group_key: str,
    window_id_key: str,
    target_group: str,
    baseline_group: str,
    terminal_orders_key: str,
    successful_orders_key: str,
    not_paid_orders_key: str,
    success_rate_key: str,
    dimension_labels: Mapping[str, str],
    result_refs: tuple[str, ...] = (),
    evidence_ref: str | None = None,
):
    """Compare reviewed final payment outcomes at aggregate dimension grain."""

    names = {
        "group_key": group_key,
        "window_id_key": window_id_key,
        "target_group": target_group,
        "baseline_group": baseline_group,
        "terminal_orders_key": terminal_orders_key,
        "successful_orders_key": successful_orders_key,
        "not_paid_orders_key": not_paid_orders_key,
        "success_rate_key": success_rate_key,
    }
    for field, value in names.items():
        if not isinstance(value, str) or not value or value != value.strip():
            raise PaymentOutcomeCompareError(f"payment_outcome_{field}_invalid")
    if target_group == baseline_group:
        raise PaymentOutcomeCompareError("payment_outcome_groups_not_distinct")
    if not isinstance(rows_by_dimension, Mapping) or not rows_by_dimension:
        raise PaymentOutcomeCompareError("payment_outcome_rows_invalid")
    if not isinstance(dimension_labels, Mapping):
        raise PaymentOutcomeCompareError("payment_outcome_dimension_labels_invalid")

    profiles = []
    dimension_summaries = []
    dimension_numeric_facts: dict[str, Any] = {}
    totals_by_dimension: dict[
        str,
        dict[tuple[str, str], dict[str, int]],
    ] = {}
    observation_count = 0
    for dimension_id, raw_rows in sorted(rows_by_dimension.items()):
        if (
            not isinstance(dimension_id, str)
            or not dimension_id
            or dimension_id not in dimension_labels
            or isinstance(raw_rows, (str, bytes))
            or not isinstance(raw_rows, Sequence)
            or not raw_rows
        ):
            raise PaymentOutcomeCompareError("payment_outcome_dimension_invalid")
        label = dimension_labels[dimension_id]
        if not isinstance(label, str) or not label:
            raise PaymentOutcomeCompareError(
                "payment_outcome_dimension_label_invalid"
            )
        observations = []
        dimension_totals: dict[tuple[str, str], dict[str, int]] = {}
        for raw in raw_rows:
            if not isinstance(raw, Mapping) or dimension_id not in raw:
                raise PaymentOutcomeCompareError("payment_outcome_row_shape_invalid")
            group = str(raw.get(group_key) or "")
            if group not in {target_group, baseline_group}:
                raise PaymentOutcomeCompareError("payment_outcome_group_invalid")
            window_id = str(raw.get(window_id_key) or "")
            member = str(raw.get(dimension_id) or "")
            if not window_id or not member:
                raise PaymentOutcomeCompareError(
                    "payment_outcome_identity_invalid"
                )
            terminal = _count(raw.get(terminal_orders_key), terminal_orders_key)
            successful = _count(
                raw.get(successful_orders_key), successful_orders_key
            )
            not_paid = _count(raw.get(not_paid_orders_key), not_paid_orders_key)
            rate = _ratio(raw.get(success_rate_key), success_rate_key)
            if successful + not_paid != terminal:
                raise PaymentOutcomeCompareError(
                    "payment_outcome_component_reconciliation_failed"
                )
            expected_rate = (
                Decimal(successful) / Decimal(terminal)
                if terminal
                else Decimal(0)
            )
            if abs(rate - expected_rate) > Decimal("0.000000000001"):
                raise PaymentOutcomeCompareError(
                    "payment_outcome_rate_reconciliation_failed"
                )
            observations.append(
                {
                    "member": member,
                    "window_id": window_id,
                    "window_role": group,
                    "terminal_orders": terminal,
                    "successful_orders": successful,
                    "not_paid_as_of_snapshot_orders": not_paid,
                    "success_rate": str(rate),
                }
            )
            totals = dimension_totals.setdefault(
                (group, window_id),
                {
                    "terminal_orders": 0,
                    "successful_orders": 0,
                    "not_paid_as_of_snapshot_orders": 0,
                },
            )
            totals["terminal_orders"] += terminal
            totals["successful_orders"] += successful
            totals["not_paid_as_of_snapshot_orders"] += not_paid
        observations.sort(
            key=lambda item: (
                item["member"],
                item["window_role"],
                item["window_id"],
            )
        )
        observation_count += len(observations)
        observations_by_member: dict[str, dict[str, Mapping[str, Any]]] = {}
        for observation in observations:
            member_observations = observations_by_member.setdefault(
                str(observation["member"]), {}
            )
            role = str(observation["window_role"])
            if role in member_observations:
                raise PaymentOutcomeCompareError(
                    "payment_outcome_member_window_duplicated"
                )
            member_observations[role] = observation
        comparable_members = tuple(
            member
            for member, member_observations in observations_by_member.items()
            if set(member_observations) == {baseline_group, target_group}
        )
        if not comparable_members:
            raise PaymentOutcomeCompareError(
                "payment_outcome_dimension_comparison_incomplete"
            )
        representative_member = sorted(
            comparable_members,
            key=lambda member: (
                -int(
                    observations_by_member[member][target_group][
                        "terminal_orders"
                    ]
                ),
                member,
            ),
        )[0]
        representative = observations_by_member[representative_member]
        baseline_observation = representative[baseline_group]
        target_observation = representative[target_group]
        dimension_summaries.append(
            {
                "dimension_id": dimension_id,
                "business_name": label,
                "representative_member": representative_member,
                "selection_policy": "largest_target_terminal_order_volume",
                "baseline": baseline_observation,
                "target": target_observation,
            }
        )
        fact_prefix = f"dimension_{dimension_id}"
        dimension_numeric_facts[
            f"{fact_prefix}_representative_member"
        ] = representative_member
        for window_role, observation in (
            ("baseline", baseline_observation),
            ("target", target_observation),
        ):
            dimension_numeric_facts[
                f"{fact_prefix}_{window_role}_terminal_payment_orders"
            ] = observation["terminal_orders"]
            dimension_numeric_facts[
                f"{fact_prefix}_{window_role}_successful_payment_orders"
            ] = observation["successful_orders"]
            dimension_numeric_facts[
                f"{fact_prefix}_{window_role}_not_paid_payment_orders"
            ] = observation["not_paid_as_of_snapshot_orders"]
            dimension_numeric_facts[
                f"{fact_prefix}_{window_role}_payment_success_rate"
            ] = float(str(observation["success_rate"]))
        totals_by_dimension[dimension_id] = dimension_totals
        profiles.append(
            {
                "dimension_id": dimension_id,
                "business_name": label,
                "observations": tuple(observations),
            }
        )

    canonical_totals: dict[tuple[str, str], dict[str, int]] | None = None
    for dimension_id in sorted(totals_by_dimension):
        totals = totals_by_dimension[dimension_id]
        if canonical_totals is None:
            canonical_totals = totals
            continue
        if totals != canonical_totals:
            raise PaymentOutcomeCompareError(
                "payment_outcome_dimension_total_reconciliation_failed"
            )
    if canonical_totals is None:
        raise PaymentOutcomeCompareError("payment_outcome_rows_invalid")
    observed_roles = {role for role, _ in canonical_totals}
    if (
        observed_roles != {target_group, baseline_group}
        or len(canonical_totals) != 2
    ):
        raise PaymentOutcomeCompareError(
            "payment_outcome_window_totals_incomplete"
        )
    window_totals = tuple(
        {
            "window_id": window_id,
            "window_role": role,
            **totals,
            "success_rate": str(
                (
                    Decimal(totals["successful_orders"])
                    / Decimal(totals["terminal_orders"])
                )
                if totals["terminal_orders"]
                else Decimal(0)
            ),
        }
        for (role, window_id), totals in sorted(canonical_totals.items())
    )
    totals_by_role = {item["window_role"]: item for item in window_totals}
    baseline_totals = totals_by_role[baseline_group]
    target_totals = totals_by_role[target_group]

    return make_evidence_envelope(
        "payment_outcome_compare",
        evidence_type="observed_comparison",
        strength="medium",
        wording_limit="directional",
        numeric_facts={
            "dimension_count": len(profiles),
            "observation_count": observation_count,
            "baseline_terminal_payment_orders": baseline_totals["terminal_orders"],
            "baseline_successful_payment_orders": baseline_totals[
                "successful_orders"
            ],
            "baseline_not_paid_payment_orders": baseline_totals[
                "not_paid_as_of_snapshot_orders"
            ],
            "baseline_payment_success_rate": float(
                baseline_totals["success_rate"]
            ),
            "target_terminal_payment_orders": target_totals["terminal_orders"],
            "target_successful_payment_orders": target_totals["successful_orders"],
            "target_not_paid_payment_orders": target_totals[
                "not_paid_as_of_snapshot_orders"
            ],
            "target_payment_success_rate": float(target_totals["success_rate"]),
            **dimension_numeric_facts,
        },
        typed_payload={
            "evidence_contract": "payment-final-outcome-comparison.v1",
            "window_totals": window_totals,
            "profiles": tuple(profiles),
            "dimension_summaries": tuple(dimension_summaries),
            "interpretation_contract": {
                "contract_id": "payment-final-outcome-interpretation.v1",
                "outcome_observation_scope": "final_status_as_of_snapshot",
                "dimension_summary_selection_policy": (
                    "largest_target_terminal_order_volume"
                ),
                "dimension_summary_claim_scope": "representative_not_exhaustive",
                "process_inference_allowed": False,
                "causal_inference_allowed": False,
            },
            "outcome_semantics": {
                "successful": "pay_success under canonical success authority",
                "not_paid_as_of_snapshot": (
                    "no pay_success observed for the order in the frozen snapshot"
                ),
            },
            "claim_ceiling": "directional",
            "failure_reason_claim_allowed": False,
            "failure_stage_claim_allowed": False,
            "retry_or_latency_claim_allowed": False,
            "causal_claim_allowed": False,
        },
        limitations=(
            "payment_failure_reason_unavailable",
            "payment_failure_stage_unavailable",
            "payment_retry_chain_unavailable",
            "payment_processing_latency_unavailable",
        ),
        result_refs=result_refs,
        evidence_ref=evidence_ref,
    )


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise PaymentOutcomeCompareError(f"payment_outcome_{field}_invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PaymentOutcomeCompareError(
            f"payment_outcome_{field}_invalid"
        ) from exc
    if parsed < 0 or parsed != parsed.to_integral_value():
        raise PaymentOutcomeCompareError(f"payment_outcome_{field}_invalid")
    return int(parsed)


def _ratio(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PaymentOutcomeCompareError(
            f"payment_outcome_{field}_invalid"
        ) from exc
    if not Decimal(0) <= parsed <= Decimal(1):
        raise PaymentOutcomeCompareError(f"payment_outcome_{field}_invalid")
    return parsed
