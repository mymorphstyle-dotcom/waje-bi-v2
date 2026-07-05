from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope


def segment_bridge(
    segments: Iterable[dict[str, Any]] = (),
    *,
    residual: float = 0.0,
    fit: float = 1.0,
    result_refs: tuple[str, ...] = (),
):
    segments = tuple(segments)
    needs_joint_attribution = abs(residual) > 0.10 or fit < 0.80
    return make_evidence_envelope(
        "segment_bridge",
        evidence_type="contextual_evidence" if segments else "insufficient_evidence",
        strength="medium" if segments else "low",
        wording_limit="contextual" if segments else "insufficient",
        typed_payload={
            "segments": segments,
            "residual": residual,
            "fit": fit,
            "needs_joint_attribution": needs_joint_attribution,
        },
        limitations=() if segments else ("no_segment_rows",),
        result_refs=result_refs,
    )
