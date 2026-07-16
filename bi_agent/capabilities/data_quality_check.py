from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope


def data_quality_check(
    rows: Iterable[dict[str, Any]],
    *,
    required_fields: tuple[str, ...] = (),
    result_refs: tuple[str, ...] = (),
):
    rows = list(rows)
    quality_risks = _quality_risks(rows)
    risk_limitations = _risk_limitations(quality_risks)
    if not rows:
        return make_evidence_envelope(
            "data_quality_check",
            evidence_type="insufficient",
            strength="insufficient",
            wording_limit="blocked",
            typed_payload={"row_count": 0, "missing_required_fields": {}, "quality_risks": {}},
            limitations=("no_rows",),
            result_refs=result_refs,
        )
    if not required_fields:
        return make_evidence_envelope(
            "data_quality_check",
            evidence_type="insufficient",
            strength="low",
            wording_limit="degraded",
            typed_payload={
                "row_count": len(rows),
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
        evidence_type="insufficient",
        strength="high" if not failed and not risk_limitations else "low",
        wording_limit="supported" if not failed and not risk_limitations else "degraded",
        typed_payload={
            "row_count": len(rows),
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


def _quality_risks(rows: list[dict[str, Any]]) -> dict[str, Any]:
    risks = {}
    for field in ("non_success_orders", "duplicate_orders"):
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
