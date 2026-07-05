from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope


def data_quality_check(
    rows: Iterable[dict[str, Any]],
    *,
    required_fields: tuple[str, ...] = (),
    result_refs: tuple[str, ...] = (),
):
    rows = list(rows)
    if not rows:
        return make_evidence_envelope(
            "data_quality_check",
            evidence_type="insufficient",
            strength="insufficient",
            wording_limit="blocked",
            typed_payload={"row_count": 0, "missing_required_fields": {}},
            limitations=("no_rows",),
            result_refs=result_refs,
        )
    if not required_fields:
        return make_evidence_envelope(
            "data_quality_check",
            evidence_type="data_quality",
            strength="low",
            wording_limit="degraded",
            typed_payload={"row_count": len(rows), "missing_required_fields": {}},
            limitations=("no_required_fields_checked",),
            result_refs=result_refs,
        )
    missing = {
        field: sum(1 for row in rows if row.get(field) is None)
        for field in required_fields
    }
    failed = {field: count for field, count in missing.items() if count}
    return make_evidence_envelope(
        "data_quality_check",
        evidence_type="data_quality",
        strength="high" if not failed else "low",
        wording_limit="supported" if not failed else "degraded",
        typed_payload={"row_count": len(rows), "missing_required_fields": failed},
        limitations=tuple(f"missing_required_field:{field}" for field in failed),
        result_refs=result_refs,
    )


check_data_quality = data_quality_check
