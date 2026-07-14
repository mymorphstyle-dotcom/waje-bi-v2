from __future__ import annotations

from typing import Any, Mapping, Sequence

from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)


PHYSICAL_QUERY_REUSE_DECISION_SCHEMA_VERSION = "physical-query-reuse-decision.v1"
PHYSICAL_QUERY_REUSE_DECISION_AUTHORITY_FIELDS = frozenset(
    {"schema_version", "decision_ref", "decision_digest"}
)

_PHYSICAL_QUERY_REUSE_DECISION_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "topic_id",
        "analysis_contract_ref",
        "source_run_id",
        "source_analysis_contract_ref",
        "source_ref",
        "source_query_contract_ref",
        "source_query_execution_record_ref",
        "source_completeness_record_refs",
        "result_ref",
        "query_contract_ref",
        "query_contract_signature",
        "query_execution_record_ref",
        "completeness_record_refs",
        "candidate_signature",
        "decision",
        "reason",
        "can_support_claim",
        "requires_rerun",
    }
)

_PHYSICAL_QUERY_REUSE_DECISION_FIELDS = frozenset(
    {
        *_PHYSICAL_QUERY_REUSE_DECISION_PAYLOAD_FIELDS,
        "decision_ref",
        "decision_digest",
    }
)

_LEGACY_QUERY_REUSE_DECISION_COMPACT_FIELDS = (
    frozenset({"source_ref", "decision"}),
    frozenset({"source_ref", "result_ref", "decision"}),
)
_LEGACY_QUERY_REUSE_DECISION_FULL_FIELDS = frozenset(
    {
        "source_ref",
        "result_ref",
        "decision",
        "reason",
        "can_support_claim",
        "requires_rerun",
    }
)


def build_physical_query_reuse_decision_record(
    *,
    run_id: str,
    topic_id: str,
    analysis_contract_ref: str,
    source_run_id: str,
    source_analysis_contract_ref: str,
    source_ref: str,
    source_query_contract_ref: str,
    source_query_execution_record_ref: str,
    source_completeness_record_refs: Sequence[str],
    result_ref: str,
    query_contract_ref: str,
    query_contract_signature: str,
    query_execution_record_ref: str,
    completeness_record_refs: Sequence[str],
    candidate_signature: str,
    decision: str,
    reason: str,
) -> dict[str, Any]:
    current_owner = {
        "run_id": run_id,
        "topic_id": topic_id,
        "analysis_contract_ref": analysis_contract_ref,
        "result_ref": result_ref,
        "query_contract_ref": query_contract_ref,
        "query_contract_signature": query_contract_signature,
        "query_execution_record_ref": query_execution_record_ref,
    }
    if any(
        not isinstance(value, str) or not value
        for value in current_owner.values()
    ):
        raise EvidenceIntegrityError("physical_reuse_decision_owner_missing")
    if decision not in {"reuse", "rerun"}:
        raise EvidenceIntegrityError("physical_reuse_decision_value_invalid")
    if not isinstance(reason, str) or not reason:
        raise EvidenceIntegrityError("physical_reuse_decision_reason_missing")

    current_completeness = _normalized_refs(
        completeness_record_refs,
        field="completeness_record_refs",
        required=True,
    )
    source_completeness = _normalized_refs(
        source_completeness_record_refs,
        field="source_completeness_record_refs",
        required=decision == "reuse",
    )
    source_owner = {
        "source_run_id": source_run_id,
        "source_analysis_contract_ref": source_analysis_contract_ref,
        "source_ref": source_ref,
        "source_query_contract_ref": source_query_contract_ref,
        "source_query_execution_record_ref": source_query_execution_record_ref,
        "candidate_signature": candidate_signature,
    }
    if any(not isinstance(value, str) for value in source_owner.values()):
        raise EvidenceIntegrityError("physical_reuse_decision_source_invalid")
    if not source_run_id:
        raise EvidenceIntegrityError("physical_reuse_decision_source_run_missing")
    if source_run_id == run_id:
        raise EvidenceIntegrityError("physical_reuse_decision_source_run_alias")
    if decision == "reuse":
        if any(not value for value in source_owner.values()):
            raise EvidenceIntegrityError("physical_reuse_decision_source_missing")
        if source_ref == result_ref:
            raise EvidenceIntegrityError("physical_reuse_decision_result_alias")
        if reason != "validated_authoritative_query_chain":
            raise EvidenceIntegrityError("physical_reuse_decision_reuse_reason_invalid")

    payload = canonical_value(
        {
            "schema_version": PHYSICAL_QUERY_REUSE_DECISION_SCHEMA_VERSION,
            **current_owner,
            **source_owner,
            "source_completeness_record_refs": source_completeness,
            "completeness_record_refs": current_completeness,
            "decision": decision,
            "reason": reason,
            "can_support_claim": decision == "reuse",
            "requires_rerun": decision == "rerun",
        }
    )
    digest = canonical_digest(payload)
    return {
        **payload,
        "decision_ref": f"reuse-decision:sha256:{digest}",
        "decision_digest": digest,
    }


def validate_physical_query_reuse_decision_record(
    record: Mapping[str, Any],
) -> None:
    validated_physical_query_reuse_decision_record(record)


def validated_physical_query_reuse_decision_record(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(record, Mapping) or set(record) != (
        _PHYSICAL_QUERY_REUSE_DECISION_FIELDS
    ):
        raise EvidenceIntegrityError("physical_reuse_decision_payload_keys_invalid")
    if record.get("schema_version") != (
        PHYSICAL_QUERY_REUSE_DECISION_SCHEMA_VERSION
    ):
        raise EvidenceIntegrityError("physical_reuse_decision_schema_version_invalid")
    rebuilt = build_physical_query_reuse_decision_record(
        run_id=str(record.get("run_id") or ""),
        topic_id=str(record.get("topic_id") or ""),
        analysis_contract_ref=str(record.get("analysis_contract_ref") or ""),
        source_run_id=str(record.get("source_run_id") or ""),
        source_analysis_contract_ref=str(
            record.get("source_analysis_contract_ref") or ""
        ),
        source_ref=str(record.get("source_ref") or ""),
        source_query_contract_ref=str(
            record.get("source_query_contract_ref") or ""
        ),
        source_query_execution_record_ref=str(
            record.get("source_query_execution_record_ref") or ""
        ),
        source_completeness_record_refs=(
            record.get("source_completeness_record_refs") or ()
        ),
        result_ref=str(record.get("result_ref") or ""),
        query_contract_ref=str(record.get("query_contract_ref") or ""),
        query_contract_signature=str(
            record.get("query_contract_signature") or ""
        ),
        query_execution_record_ref=str(
            record.get("query_execution_record_ref") or ""
        ),
        completeness_record_refs=record.get("completeness_record_refs") or (),
        candidate_signature=str(record.get("candidate_signature") or ""),
        decision=str(record.get("decision") or ""),
        reason=str(record.get("reason") or ""),
    )
    if canonical_value(record) != canonical_value(rebuilt):
        raise EvidenceIntegrityError("physical_reuse_decision_integrity_invalid")
    return dict(rebuilt)


def physical_reuse_decision_cache_provenance_matches(
    decision: Mapping[str, Any],
    provider_stats: Mapping[str, Any],
) -> bool:
    """Bind a final physical decision to the current query execution cache facts."""
    if not isinstance(decision, Mapping) or not isinstance(provider_stats, Mapping):
        return False
    if decision.get("decision") == "reuse":
        return (
            provider_stats.get("cache_hit") is True
            and provider_stats.get("cache_source")
            == "validated_authoritative_query_chain"
            and provider_stats.get("source_result_ref")
            == decision.get("source_ref")
            and provider_stats.get("candidate_signature")
            == decision.get("candidate_signature")
        )
    if decision.get("decision") != "rerun":
        return False
    return (
        provider_stats.get("cache_hit") is not True
        and not provider_stats.get("cache_source")
        and not provider_stats.get("source_result_ref")
        and not provider_stats.get("candidate_signature")
    )


def validated_query_reuse_decision(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Parse one exact legacy or signed physical reuse decision."""
    if not isinstance(record, Mapping):
        raise EvidenceIntegrityError("reuse_decision_shape_invalid")
    keys = frozenset(record)
    if keys == _PHYSICAL_QUERY_REUSE_DECISION_FIELDS:
        return validated_physical_query_reuse_decision_record(record)
    if keys not in (
        *_LEGACY_QUERY_REUSE_DECISION_COMPACT_FIELDS,
        _LEGACY_QUERY_REUSE_DECISION_FULL_FIELDS,
    ):
        raise EvidenceIntegrityError("reuse_decision_shape_invalid")

    source_ref = record.get("source_ref")
    result_ref = record.get("result_ref", "")
    decision = record.get("decision")
    if (
        not isinstance(source_ref, str)
        or not source_ref
        or not isinstance(result_ref, str)
        or not isinstance(decision, str)
        or not decision
    ):
        raise EvidenceIntegrityError("reuse_decision_legacy_invalid")
    if keys == _LEGACY_QUERY_REUSE_DECISION_FULL_FIELDS and (
        not isinstance(record.get("reason"), str)
        or not isinstance(record.get("can_support_claim"), bool)
        or not isinstance(record.get("requires_rerun"), bool)
    ):
        raise EvidenceIntegrityError("reuse_decision_legacy_invalid")
    return {
        "source_ref": source_ref,
        **({"result_ref": result_ref} if result_ref else {}),
        "decision": decision,
    }


def _normalized_refs(
    values: Sequence[str],
    *,
    field: str,
    required: bool,
) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise EvidenceIntegrityError(f"physical_reuse_decision_{field}_invalid")
    refs = list(values)
    if any(not isinstance(item, str) or not item for item in refs):
        raise EvidenceIntegrityError(f"physical_reuse_decision_{field}_invalid")
    if len(refs) != len(set(refs)):
        raise EvidenceIntegrityError(f"physical_reuse_decision_{field}_duplicate")
    if required and not refs:
        raise EvidenceIntegrityError(f"physical_reuse_decision_{field}_missing")
    return refs
