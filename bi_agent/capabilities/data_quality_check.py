from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope


def data_quality_check(
    rows: Iterable[dict[str, Any]],
    *,
    required_fields: tuple[str, ...] = (),
    source_row_count_key: str = "source_row_count",
    result_refs: tuple[str, ...] = (),
):
    rows = list(rows)
    measurement = _measurement(rows, source_row_count_key=source_row_count_key)
    quality_risks = _quality_risks(rows)
    risk_limitations = _risk_limitations(quality_risks)
    if not rows:
        return make_evidence_envelope(
            "data_quality_check",
            evidence_type="insufficient_evidence",
            strength="insufficient",
            wording_limit="blocked",
            typed_payload={
                **measurement,
                "missing_required_fields": {},
                "quality_risks": {},
            },
            limitations=("no_rows",),
            result_refs=result_refs,
        )
    if not required_fields:
        return make_evidence_envelope(
            "data_quality_check",
            evidence_type="trust_boundary",
            strength="trust_boundary",
            wording_limit="degraded",
            typed_payload={
                **measurement,
                "missing_required_fields": {},
                "quality_risks": quality_risks,
            },
            limitations=("no_required_fields_checked", *risk_limitations),
            result_refs=result_refs,
        )
    missing = {
        field: sum(1 for row in rows if row.get(field) is None)
        for field in required_fields
    }
    failed = {field: count for field, count in missing.items() if count}
    return make_evidence_envelope(
        "data_quality_check",
        evidence_type="trust_boundary",
        strength="trust_boundary",
        wording_limit="supported"
        if not failed and not risk_limitations
        else "degraded",
        typed_payload={
            **measurement,
            "missing_required_fields": failed,
            "quality_risks": quality_risks,
        },
        limitations=(
            *(f"missing_required_field:{field}" for field in failed),
            *risk_limitations,
        ),
        result_refs=result_refs,
    )


check_data_quality = data_quality_check


def _measurement(
    rows: list[dict[str, Any]],
    *,
    source_row_count_key: str,
) -> dict[str, Any]:
    if (
        not isinstance(source_row_count_key, str)
        or not source_row_count_key
        or source_row_count_key != source_row_count_key.strip()
    ):
        raise ValueError("data_quality_source_row_count_key_invalid")
    measurement: dict[str, Any] = {
        "result_group_count": len(rows),
        "result_group_unit": "window_aggregate",
    }
    source_counts = [
        row[source_row_count_key] for row in rows if source_row_count_key in row
    ]
    if not source_counts:
        return measurement
    if len(source_counts) != len(rows) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in source_counts
    ):
        raise ValueError("data_quality_source_coverage_count_invalid")
    measurement.update(
        {
            "source_coverage_count": sum(source_counts),
            "source_coverage_unit": "window_scoped_source_record",
        }
    )
    return measurement


def _quality_risks(rows: list[dict[str, Any]]) -> dict[str, Any]:
    risks = {}
    for field in ("non_success_orders", "duplicate_orders"):
        total = sum(_numeric(row.get(field)) for row in rows if field in row)
        if total:
            risks[field] = total
    for field in sorted(
        {
            str(key)
            for row in rows
            for key in row
            if str(key).endswith("_scope_violation_count")
        }
    ):
        total = sum(_numeric(row.get(field)) for row in rows if field in row)
        if total:
            risks[field] = total
    return risks


def _risk_limitations(quality_risks: dict[str, Any]) -> tuple[str, ...]:
    limitations = []
    if quality_risks.get("non_success_orders"):
        limitations.append("payment_status_risk")
    if quality_risks.get("duplicate_orders"):
        limitations.append("duplicate_order_risk")
    limitations.extend(
        f"scope_invariant_violation:{field.removesuffix('_scope_violation_count')}"
        for field in quality_risks
        if field.endswith("_scope_violation_count")
    )
    return tuple(limitations)


def _numeric(value: Any) -> Any:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0
