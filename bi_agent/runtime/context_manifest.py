from __future__ import annotations

from typing import Any, Mapping, Sequence

from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)


CONTEXT_MANIFEST_SCHEMA_VERSION = "3"
_CONTEXT_KEYS = {
    "run_id",
    "thread_id",
    "topic_id",
    "sources",
    "can_support_claims",
    "manifest_id",
    "manifest_digest",
    "accepted_assumptions",
    "manifest_schema_version",
}


def build_context_manifest_record(
    *,
    run_id: str,
    thread_id: str,
    topic_id: str,
    sources: Sequence[Mapping[str, Any]],
    accepted_assumptions: Sequence[Mapping[str, Any]] = (),
    can_support_claims: bool = True,
) -> dict[str, Any]:
    if not run_id or not thread_id or not topic_id:
        raise EvidenceIntegrityError("context_manifest_owner_missing")
    normalized_sources = _canonical_sources(
        sources,
        can_support_claims=can_support_claims,
    )
    if not normalized_sources:
        raise EvidenceIntegrityError("context_manifest_sources_missing")
    assumptions = tuple(accepted_assumptions)
    if any(not isinstance(item, Mapping) for item in assumptions):
        raise EvidenceIntegrityError("context_manifest_assumptions_invalid")
    payload = canonical_value(
        {
            "run_id": run_id,
            "thread_id": thread_id,
            "topic_id": topic_id,
            "sources": normalized_sources,
            "accepted_assumptions": [dict(item) for item in assumptions],
            "manifest_schema_version": CONTEXT_MANIFEST_SCHEMA_VERSION,
            "can_support_claims": can_support_claims,
        }
    )
    digest = canonical_digest(payload)
    return {
        **payload,
        "manifest_id": f"context-manifest:sha256:{digest}",
        "manifest_digest": digest,
    }


def validate_context_manifest_record(record: Mapping[str, Any]) -> None:
    validated_context_manifest_record(record)


def validated_context_manifest_record(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(record, Mapping) or set(record) != _CONTEXT_KEYS:
        raise EvidenceIntegrityError("context_manifest_payload_keys_invalid")
    if record.get("manifest_schema_version") != CONTEXT_MANIFEST_SCHEMA_VERSION:
        raise EvidenceIntegrityError("context_manifest_schema_version_invalid")
    if type(record.get("can_support_claims")) is not bool:
        raise EvidenceIntegrityError("context_manifest_claim_support_invalid")
    rebuilt = build_context_manifest_record(
        run_id=str(record.get("run_id") or ""),
        thread_id=str(record.get("thread_id") or ""),
        topic_id=str(record.get("topic_id") or ""),
        sources=record.get("sources") or (),
        accepted_assumptions=record.get("accepted_assumptions") or (),
        can_support_claims=record["can_support_claims"],
    )
    if canonical_value(record) != canonical_value(rebuilt):
        raise EvidenceIntegrityError("context_manifest_integrity_invalid")
    return dict(rebuilt)


def _canonical_sources(
    sources: Sequence[Mapping[str, Any]],
    *,
    can_support_claims: bool,
) -> list[dict[str, Any]]:
    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise EvidenceIntegrityError("context_manifest_source_invalid")
    normalized = []
    seen = set()
    for source in sources:
        if not isinstance(source, Mapping) or set(source) != {
            "type",
            "ref",
            "can_support_claim",
        }:
            raise EvidenceIntegrityError("context_manifest_source_invalid")
        source_type = source.get("type")
        ref = source.get("ref")
        allowed_types = {"evidence", "completeness"}
        if not can_support_claims:
            allowed_types.add("limitation")
        if (
            type(source_type) is not str
            or source_type not in allowed_types
            or type(ref) is not str
            or not ref
            or ref != ref.strip()
            or type(source.get("can_support_claim")) is not bool
        ):
            raise EvidenceIntegrityError("context_manifest_source_invalid")
        if source["can_support_claim"] is not can_support_claims:
            raise EvidenceIntegrityError("context_manifest_source_not_claim_ready")
        key = (source_type, ref)
        if key not in seen:
            seen.add(key)
            normalized.append(
                {
                    "type": source_type,
                    "ref": ref,
                    "can_support_claim": source["can_support_claim"],
                }
            )
    return normalized


__all__ = (
    "CONTEXT_MANIFEST_SCHEMA_VERSION",
    "build_context_manifest_record",
    "validate_context_manifest_record",
    "validated_context_manifest_record",
)
