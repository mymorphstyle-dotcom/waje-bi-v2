from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope


SENSITIVE_KEYS = frozenset(
    {
        "account_id",
        "device_id",
        "email",
        "ip",
        "phone",
        "raw_device_id",
        "raw_ip",
        "raw_user_id",
        "user_id",
    }
)
SAFE_SEGMENT_KEYS = frozenset(
    {
        "amount",
        "delta",
        "fit",
        "n",
        "order_count",
        "orders",
        "paid_users",
        "segment",
        "share",
        "user_count",
    }
)
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
    has_sensitive = any(_has_sensitive_keys(segment) for segment in segments)
    sample_sizes = tuple(_sample_size(segment) for segment in segments)
    has_unverified_sample = any(size is None for size in sample_sizes)
    has_sparse = any(size is not None and size < SPARSE_THRESHOLD for size in sample_sizes)
    if has_sensitive or has_sparse or has_unverified_sample:
        limitations = tuple(
            reason
            for reason, present in (
                ("raw_identifier_present", has_sensitive),
                ("sparse_cell", has_sparse),
                ("sample_size_unverified", has_unverified_sample),
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
            "segments": tuple(_safe_segment(segment) for segment in segments),
            "residual": residual,
            "fit": fit,
            "needs_joint_attribution": needs_joint_attribution,
        },
        limitations=() if segments else ("no_segment_rows",),
        result_refs=result_refs,
    )


def _sample_size(segment: dict[str, Any]):
    found = False
    for key in ("n", "sample_size", "order_count", "orders", "user_count", "paid_users"):
        value = segment.get(key)
        if value is not None:
            found = True
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    if not found:
        return None
    return None


def _has_sensitive_keys(segment: dict[str, Any]) -> bool:
    for key in segment:
        normalized = str(key).lower()
        if normalized in SENSITIVE_KEYS:
            return True
        if any(token in normalized for token in ("email", "phone", "account_id", "user_id", "device_id", "raw_ip")):
            return True
    return False


def _safe_segment(segment: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in segment.items() if key in SAFE_SEGMENT_KEYS}
