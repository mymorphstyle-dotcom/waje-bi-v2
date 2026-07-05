from statistics import median
from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope


def outlier_scan(
    rows: Iterable[dict[str, Any]],
    *,
    value_key: str = "amount",
    result_refs: tuple[str, ...] = (),
):
    values = [
        value
        for value in (_as_number(row.get(value_key)) for row in rows)
        if value is not None
    ]
    if len(values) < 3:
        return make_evidence_envelope(
            "outlier_scan",
            evidence_type="insufficient_evidence",
            strength="low",
            wording_limit="insufficient",
            typed_payload={"outliers": (), "value_count": len(values)},
            limitations=("insufficient_values",),
            result_refs=result_refs,
        )
    center = median(values)
    deviations = [abs(value - center) for value in values]
    mad = median(deviations)
    if mad == 0:
        outliers = tuple(value for value in values if value != center)
    else:
        outliers = tuple(value for value in values if abs(value - center) / mad > 6)
    return make_evidence_envelope(
        "outlier_scan",
        evidence_type="contextual_evidence",
        strength="medium" if outliers else "low",
        wording_limit="contextual" if outliers else "none_found",
        typed_payload={"outliers": outliers, "value_count": len(values), "median": center},
        limitations=(),
        result_refs=result_refs,
    )


scan_outliers = outlier_scan


def _as_number(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
