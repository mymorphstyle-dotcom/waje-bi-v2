from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value


class SourceMetricReconciliationError(ValueError):
    pass


@dataclass(frozen=True)
class SourceMetricObservation:
    business_date: str
    authority_amount: Decimal
    comparison_amount: Decimal
    amount_difference: Decimal
    authority_users: int
    comparison_users: int
    user_difference: int
    amount_status: str
    user_status: str
    comparison_claim_ceiling: str

    @classmethod
    def create(
        cls,
        *,
        business_date: str,
        authority_amount: Any,
        comparison_amount: Any,
        authority_users: Any,
        comparison_users: Any,
        amount_tolerance: Decimal = Decimal("0.01"),
    ) -> "SourceMetricObservation":
        try:
            date.fromisoformat(business_date)
        except (TypeError, ValueError) as exc:
            raise SourceMetricReconciliationError(
                "source_metric_business_date_invalid"
            ) from exc
        authority = _decimal(authority_amount, "authority_amount")
        comparison = _decimal(comparison_amount, "comparison_amount")
        authority_count = _count(authority_users, "authority_users")
        comparison_count = _count(comparison_users, "comparison_users")
        tolerance = _decimal(amount_tolerance, "amount_tolerance")
        if tolerance < 0:
            raise SourceMetricReconciliationError(
                "source_metric_amount_tolerance_invalid"
            )
        amount_difference = comparison - authority
        user_difference = comparison_count - authority_count
        amount_status = (
            "matched" if abs(amount_difference) <= tolerance else "mismatch"
        )
        user_status = "matched" if user_difference == 0 else "mismatch"
        return cls(
            business_date=business_date,
            authority_amount=authority,
            comparison_amount=comparison,
            amount_difference=amount_difference,
            authority_users=authority_count,
            comparison_users=comparison_count,
            user_difference=user_difference,
            amount_status=amount_status,
            user_status=user_status,
            comparison_claim_ceiling=(
                "observed"
                if amount_status == "matched" and user_status == "matched"
                else "context_only"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(asdict(self))


@dataclass(frozen=True)
class SourceMetricReconciliationReport:
    schema_version: str
    report_ref: str
    content_digest: str
    authority_dataset_id: str
    comparison_dataset_id: str
    business_timezone: str
    amount_tolerance: Decimal
    overall_status: str
    authority_action: str
    observations: tuple[SourceMetricObservation, ...]

    @classmethod
    def create(
        cls,
        *,
        authority_dataset_id: str,
        comparison_dataset_id: str,
        business_timezone: str,
        amount_tolerance: Decimal,
        observations: Sequence[SourceMetricObservation],
    ) -> "SourceMetricReconciliationReport":
        if (
            not isinstance(authority_dataset_id, str)
            or not authority_dataset_id.strip()
            or not isinstance(comparison_dataset_id, str)
            or not comparison_dataset_id.strip()
            or authority_dataset_id == comparison_dataset_id
            or business_timezone != "Africa/Lagos"
        ):
            raise SourceMetricReconciliationError(
                "source_metric_reconciliation_identity_invalid"
            )
        typed = tuple(observations)
        if (
            not typed
            or any(type(item) is not SourceMetricObservation for item in typed)
            or len({item.business_date for item in typed}) != len(typed)
        ):
            raise SourceMetricReconciliationError(
                "source_metric_reconciliation_observations_invalid"
            )
        typed = tuple(sorted(typed, key=lambda item: item.business_date))
        tolerance = _decimal(amount_tolerance, "amount_tolerance")
        overall_status = (
            "matched"
            if all(
                item.amount_status == "matched" and item.user_status == "matched"
                for item in typed
            )
            else "mismatch"
        )
        authority_action = (
            "sources_interchangeable_for_reconciled_fields"
            if overall_status == "matched"
            else "retain_authority_and_limit_comparison_source_to_context"
        )
        body = {
            "schema_version": "source-metric-reconciliation.v1",
            "authority_dataset_id": authority_dataset_id,
            "comparison_dataset_id": comparison_dataset_id,
            "business_timezone": business_timezone,
            "amount_tolerance": tolerance,
            "overall_status": overall_status,
            "authority_action": authority_action,
            "observations": [item.to_dict() for item in typed],
        }
        digest = canonical_digest(body)
        return cls(
            report_ref="source-metric-reconciliation:sha256:" + digest,
            content_digest=digest,
            observations=typed,
            **{key: value for key, value in body.items() if key != "observations"},
        )

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(asdict(self))


def report_from_records(
    records: Sequence[Mapping[str, Any]],
    *,
    authority_dataset_id: str,
    comparison_dataset_id: str,
    business_timezone: str = "Africa/Lagos",
    amount_tolerance: Decimal = Decimal("0.01"),
) -> SourceMetricReconciliationReport:
    observations = tuple(
        SourceMetricObservation.create(
            business_date=str(item.get("business_date") or ""),
            authority_amount=item.get("authority_amount"),
            comparison_amount=item.get("comparison_amount"),
            authority_users=item.get("authority_users"),
            comparison_users=item.get("comparison_users"),
            amount_tolerance=amount_tolerance,
        )
        for item in records
    )
    return SourceMetricReconciliationReport.create(
        authority_dataset_id=authority_dataset_id,
        comparison_dataset_id=comparison_dataset_id,
        business_timezone=business_timezone,
        amount_tolerance=amount_tolerance,
        observations=observations,
    )


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SourceMetricReconciliationError(
            f"source_metric_decimal_invalid:{field}"
        ) from exc
    if not parsed.is_finite():
        raise SourceMetricReconciliationError(
            f"source_metric_decimal_invalid:{field}"
        )
    return parsed


def _count(value: Any, field: str) -> int:
    if type(value) is int and value >= 0:
        return value
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise SourceMetricReconciliationError(
            f"source_metric_count_invalid:{field}"
        ) from exc
    if parsed < 0 or Decimal(str(value)) != Decimal(parsed):
        raise SourceMetricReconciliationError(
            f"source_metric_count_invalid:{field}"
        )
    return parsed
