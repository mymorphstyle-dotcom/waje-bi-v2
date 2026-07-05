from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope


SENSITIVE_KEYS = frozenset({"raw_user_id", "user_id", "raw_ip", "ip", "raw_device_id", "device_id"})
SPARSE_THRESHOLD = 10


def segment_bridge(
    segments: Iterable[dict[str, Any]] = (),
    *,
    residual: float = 0.0,
    fit: float = 1.0,
    result_refs: tuple[str, ...] = (),
):
    segments = tuple(segments)
    needs_joint_attribution = abs(residual) > 0.10 or fit < 0.80
    has_sensitive = any(set(segment) & SENSITIVE_KEYS for segment in segments)
    has_sparse = any(_sample_size(segment) is not None and _sample_size(segment) < SPARSE_THRESHOLD for segment in segments)
    if has_sensitive or has_sparse:
        limitations = tuple(
            reason
            for reason, present in (
                ("raw_identifier_present", has_sensitive),
                ("sparse_cell", has_sparse),
            )
            if present
        )
        return make_evidence_envelope(
            "segment_bridge",
            evidence_type="permission_limited",
            strength="insufficient",
            wording_limit="blocked",
            typed_payload={
                "segment_count": len(segments),
                "residual": residual,
                "fit": fit,
                "needs_joint_attribution": needs_joint_attribution,
            },
            limitations=limitations,
            result_refs=result_refs,
        )

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


def _sample_size(segment: dict[str, Any]):
    for key in ("n", "sample_size", "order_count", "user_count"):
        value = segment.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None
