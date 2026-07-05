from typing import Any, Iterable, Optional

from bi_agent.capabilities import make_evidence_envelope


def joint_attribution(
    rows: Iterable[dict[str, Any]] = (),
    *,
    segment_evidence: Optional[Any] = None,
    residual: float = 0.0,
    fit: float = 1.0,
    result_refs: tuple[str, ...] = (),
):
    if segment_evidence is not None:
        payload = getattr(segment_evidence, "typed_payload", {})
        residual = payload.get("residual", residual)
        fit = payload.get("fit", fit)
    needs_escalation = abs(residual) > 0.10 or fit < 0.80
    if segment_evidence is None:
        return make_evidence_envelope(
            "joint_attribution",
            evidence_type="insufficient_evidence",
            strength="low",
            wording_limit="blocked",
            typed_payload={"residual": residual, "fit": fit},
            limitations=("segment_bridge_required",),
            result_refs=result_refs,
        )
    if not needs_escalation:
        return make_evidence_envelope(
            "joint_attribution",
            evidence_type="contextual_evidence",
            strength="low",
            wording_limit="not_run",
            typed_payload={"residual": residual, "fit": fit, "reason": "no_escalation_required"},
            limitations=("joint_attribution_not_required",),
            result_refs=result_refs,
        )

    rows = tuple(rows)
    return make_evidence_envelope(
        "joint_attribution",
        evidence_type="statistical_association" if rows else "insufficient_evidence",
        strength="medium" if rows else "low",
        wording_limit="candidate" if rows else "insufficient",
        typed_payload={"rows": rows, "residual": residual, "fit": fit},
        limitations=() if rows else ("no_joint_rows",),
        result_refs=result_refs,
    )
