from __future__ import annotations

from typing import Any, Mapping, Sequence

from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)


_UNTRUSTED_PROVENANCE_FIELDS = frozenset(
    {
        "claim_ref",
        "claim_id",
        "claim_digest",
        "context_manifest_ref",
        "result_refs",
        "completeness_record_refs",
        "artifact_refs",
        "memory_refs",
        "reuse_decisions",
        "provenance_record_ref",
    }
)
_CONTEXT_MANIFEST_SCHEMA_VERSION = "2"
_LEGACY_CONTEXT_KEYS = {
    "run_id", "thread_id", "topic_id", "sources", "permission_context",
    "can_support_claims", "manifest_id", "manifest_digest",
}
_CURRENT_CONTEXT_KEYS = {
    *_LEGACY_CONTEXT_KEYS,
    "accepted_assumptions",
    "manifest_schema_version",
}


def build_context_manifest_record(
    *,
    run_id: str,
    thread_id: str,
    topic_id: str,
    sources: Sequence[Mapping[str, Any]],
    permission_context: Mapping[str, Any] | None = None,
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
    payload = canonical_value(
        {
            "run_id": run_id,
            "thread_id": thread_id,
            "topic_id": topic_id,
            "sources": normalized_sources,
            "permission_context": dict(permission_context or {}),
            "accepted_assumptions": [
                dict(item) for item in accepted_assumptions if isinstance(item, Mapping)
            ],
            "manifest_schema_version": _CONTEXT_MANIFEST_SCHEMA_VERSION,
            "can_support_claims": can_support_claims,
        }
    )
    digest = canonical_digest(payload)
    return {
        **payload,
        "manifest_id": f"context-manifest:sha256:{digest}",
        "manifest_digest": digest,
    }


def build_trusted_claim_provenance_record(
    *,
    run_id: str,
    artifact_refs: Sequence[str] = (),
    memory_refs: Sequence[str] = (),
    reuse_decisions: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    if not run_id:
        raise EvidenceIntegrityError("claim_provenance_run_missing")
    normalized_reuse = []
    for raw in reuse_decisions:
        if not isinstance(raw, Mapping):
            raise EvidenceIntegrityError("claim_provenance_reuse_invalid")
        decision = {
            key: str(raw.get(key) or "")
            for key in ("source_ref", "result_ref", "decision")
            if raw.get(key)
        }
        if not decision.get("source_ref") or not decision.get("decision"):
            raise EvidenceIntegrityError("claim_provenance_reuse_invalid")
        normalized_reuse.append(decision)
    payload = canonical_value(
        {
            "run_id": run_id,
            "artifact_refs": _dedupe_refs(artifact_refs),
            "memory_refs": _dedupe_refs(memory_refs),
            "reuse_decisions": normalized_reuse,
        }
    )
    digest = canonical_digest(payload)
    return {
        **payload,
        "record_ref": f"claim-provenance:sha256:{digest}",
        "record_digest": digest,
    }


def build_verified_claim_record(
    factual_claim: Mapping[str, Any],
    *,
    run_id: str,
    context_manifest: Mapping[str, Any],
    evidence_by_ref: Mapping[str, Mapping[str, Any]],
    trusted_provenance: Mapping[str, Any],
) -> dict[str, Any]:
    validate_context_manifest_record(context_manifest)
    validate_trusted_claim_provenance_record(trusted_provenance)
    if (
        str(context_manifest.get("run_id") or "") != run_id
        or str(trusted_provenance.get("run_id") or "") != run_id
    ):
        raise EvidenceIntegrityError("verified_claim_run_mismatch")
    evidence_refs = _dedupe_refs(factual_claim.get("evidence_refs") or ())
    if not evidence_refs or any(ref not in evidence_by_ref for ref in evidence_refs):
        raise EvidenceIntegrityError("verified_claim_evidence_missing")
    evidence_items = tuple(evidence_by_ref[ref] for ref in evidence_refs)
    result_refs = _dedupe_refs(
        ref
        for item in evidence_items
        for ref in item.get("result_refs") or ()
    )
    completeness_refs = _dedupe_refs(
        ref
        for item in evidence_items
        for ref in item.get("completeness_record_refs") or ()
    )
    expected_sources = _canonical_sources(
        (
            *(
                {"type": "evidence", "ref": ref, "can_support_claim": True}
                for ref in evidence_refs
            ),
            *(
                {"type": "completeness", "ref": ref, "can_support_claim": True}
                for ref in completeness_refs
            ),
        )
    )
    manifest_sources = _canonical_sources(context_manifest.get("sources") or ())
    if any(source not in manifest_sources for source in expected_sources):
        raise EvidenceIntegrityError("verified_claim_context_sources_missing")
    factual = {
        str(key): canonical_value(value)
        for key, value in factual_claim.items()
        if key not in _UNTRUSTED_PROVENANCE_FIELDS
    }
    payload = canonical_value(
        {
            **factual,
            "run_id": run_id,
            "context_manifest_ref": context_manifest["manifest_id"],
            "evidence_refs": evidence_refs,
            "result_refs": result_refs,
            "completeness_record_refs": completeness_refs,
            "artifact_refs": trusted_provenance["artifact_refs"],
            "memory_refs": trusted_provenance["memory_refs"],
            "reuse_decisions": trusted_provenance["reuse_decisions"],
            "provenance_record_ref": trusted_provenance["record_ref"],
        }
    )
    digest = canonical_digest(payload)
    return {
        **payload,
        "claim_ref": f"claim:sha256:{digest}",
        "claim_digest": digest,
    }


def validate_context_manifest_record(record: Mapping[str, Any]) -> None:
    validated_context_manifest_record(record)


def validated_context_manifest_record(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    keys = set(record)
    if keys == _LEGACY_CONTEXT_KEYS and "manifest_schema_version" not in record:
        payload = canonical_value(
            {
                key: record[key]
                for key in (
                    "run_id", "thread_id", "topic_id", "sources",
                    "permission_context", "can_support_claims",
                )
            }
        )
        digest = canonical_digest(payload)
        expected = {
            **payload,
            "manifest_id": f"context-manifest:sha256:{digest}",
            "manifest_digest": digest,
        }
        if canonical_value(record) != canonical_value(expected):
            raise EvidenceIntegrityError("context_manifest_integrity_invalid")
        return {**dict(expected), "accepted_assumptions": []}
    if keys != _CURRENT_CONTEXT_KEYS:
        raise EvidenceIntegrityError("context_manifest_payload_keys_invalid")
    if record.get("manifest_schema_version") != _CONTEXT_MANIFEST_SCHEMA_VERSION:
        raise EvidenceIntegrityError("context_manifest_schema_version_invalid")
    rebuilt = build_context_manifest_record(
        run_id=str(record.get("run_id") or ""),
        thread_id=str(record.get("thread_id") or ""),
        topic_id=str(record.get("topic_id") or ""),
        sources=record.get("sources") or (),
        permission_context=record.get("permission_context") or {},
        accepted_assumptions=record.get("accepted_assumptions") or (),
        can_support_claims=record.get("can_support_claims") is True,
    )
    if canonical_value(record) != canonical_value(rebuilt):
        raise EvidenceIntegrityError("context_manifest_integrity_invalid")
    return dict(rebuilt)


def validate_trusted_claim_provenance_record(record: Mapping[str, Any]) -> None:
    expected_keys = {
        "run_id", "artifact_refs", "memory_refs", "reuse_decisions",
        "record_ref", "record_digest",
    }
    if set(record) != expected_keys:
        raise EvidenceIntegrityError("claim_provenance_payload_keys_invalid")
    rebuilt = build_trusted_claim_provenance_record(
        run_id=str(record.get("run_id") or ""),
        artifact_refs=record.get("artifact_refs") or (),
        memory_refs=record.get("memory_refs") or (),
        reuse_decisions=record.get("reuse_decisions") or (),
    )
    if canonical_value(record) != canonical_value(rebuilt):
        raise EvidenceIntegrityError("claim_provenance_integrity_invalid")


def validate_verified_claim_record(
    record: Mapping[str, Any],
    *,
    context_manifest: Mapping[str, Any],
    evidence_by_ref: Mapping[str, Mapping[str, Any]],
    trusted_provenance: Mapping[str, Any],
) -> None:
    required = {"claim_ref", "claim_digest"}
    if not required.issubset(record):
        raise EvidenceIntegrityError("verified_claim_identity_missing")
    factual = {
        key: value
        for key, value in record.items()
        if key not in _UNTRUSTED_PROVENANCE_FIELDS
        and key not in {"run_id", "claim_digest"}
    }
    rebuilt = build_verified_claim_record(
        factual,
        run_id=str(record.get("run_id") or ""),
        context_manifest=context_manifest,
        evidence_by_ref=evidence_by_ref,
        trusted_provenance=trusted_provenance,
    )
    if canonical_value(record) != canonical_value(rebuilt):
        raise EvidenceIntegrityError("verified_claim_integrity_invalid")


def _canonical_sources(
    sources: Sequence[Mapping[str, Any]],
    *,
    can_support_claims: bool = True,
) -> list[dict[str, Any]]:
    normalized = []
    seen = set()
    for source in sources:
        if not isinstance(source, Mapping) or set(source) != {
            "type", "ref", "can_support_claim"
        }:
            raise EvidenceIntegrityError("context_manifest_source_invalid")
        source_type = str(source.get("type") or "")
        ref = str(source.get("ref") or "")
        allowed_types = {"evidence", "completeness"}
        if not can_support_claims:
            allowed_types.add("limitation")
        if source_type not in allowed_types or not ref:
            raise EvidenceIntegrityError("context_manifest_source_invalid")
        item = {
            "type": source_type,
            "ref": ref,
            "can_support_claim": source.get("can_support_claim") is True,
        }
        if item["can_support_claim"] is not can_support_claims:
            raise EvidenceIntegrityError("context_manifest_source_not_claim_ready")
        key = (source_type, ref)
        if key not in seen:
            seen.add(key)
            normalized.append(item)
    return normalized


def _dedupe_refs(values: Sequence[Any] | Any) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))
