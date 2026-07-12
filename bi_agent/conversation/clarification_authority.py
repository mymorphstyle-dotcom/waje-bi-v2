from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bi_agent.runtime.analysis_contracts import (
    analysis_contract_from_dict,
    analysis_contract_signature,
    stable_contract_signature,
)
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError, canonical_value


def build_clarification_outcome(
    *,
    source_run_id: str,
    thread_id: str,
    topic_id: str,
    choice: Mapping[str, Any],
) -> dict[str, Any]:
    body = {
        "source_run_id": _required(source_run_id, "source_run_id"),
        "thread_id": _required(thread_id, "thread_id"),
        "topic_id": _required(topic_id, "topic_id"),
        "choice": canonical_value(dict(choice)),
    }
    digest = stable_contract_signature(body)
    payload = {
        "outcome_ref": f"clarification-outcome:{digest}",
        **body,
    }
    payload["outcome_signature"] = stable_contract_signature(payload)
    return payload


def validate_clarification_resume_authority(
    *,
    source_run_id: str,
    thread_id: str,
    topic_id: str,
    choice: Mapping[str, Any],
    outcome_ref: str,
    analysis_contract: Mapping[str, Any],
    stored_contract_signature: str,
    analysis_run_id: str,
    run_status: str,
    run_thread_id: str,
    run_topic_id: str,
    clarification_outcome: Mapping[str, Any],
    outcome_run_id: str,
    outcome_thread_id: str,
    outcome_topic_id: str,
) -> dict[str, Any]:
    if analysis_run_id != source_run_id:
        raise EvidenceIntegrityError("clarification_resume_source_run_mismatch")
    if run_status != "waiting_for_clarification":
        raise EvidenceIntegrityError("clarification_resume_source_run_stale")
    owners = (
        run_thread_id,
        run_topic_id,
        outcome_thread_id,
        outcome_topic_id,
    )
    if owners != (thread_id, topic_id, thread_id, topic_id):
        raise EvidenceIntegrityError("clarification_resume_owner_mismatch")
    if outcome_run_id != source_run_id:
        raise EvidenceIntegrityError("clarification_resume_outcome_run_mismatch")
    contract_payload = dict(analysis_contract)
    embedded_signature = str(contract_payload.pop("contract_signature", "") or "")
    try:
        typed_contract = analysis_contract_from_dict(contract_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError("clarification_resume_contract_payload_invalid") from exc
    expected_signature = analysis_contract_signature(typed_contract)
    if (
        not stored_contract_signature
        or expected_signature != stored_contract_signature
        or (embedded_signature and embedded_signature != stored_contract_signature)
    ):
        raise EvidenceIntegrityError("clarification_resume_contract_signature_invalid")
    if typed_contract.analysis_contract_id != f"analysis:{source_run_id}:1":
        raise EvidenceIntegrityError("clarification_resume_contract_run_mismatch")

    outcome = dict(clarification_outcome)
    signature = str(outcome.pop("outcome_signature", "") or "")
    if not signature or stable_contract_signature(outcome) != signature:
        raise EvidenceIntegrityError("clarification_resume_outcome_signature_invalid")
    if str(outcome.get("outcome_ref") or "") != outcome_ref:
        raise EvidenceIntegrityError("clarification_resume_outcome_ref_mismatch")
    expected_outcome_ref = "clarification-outcome:" + stable_contract_signature(
        {
            "source_run_id": source_run_id,
            "thread_id": thread_id,
            "topic_id": topic_id,
            "choice": canonical_value(dict(choice)),
        }
    )
    if outcome_ref != expected_outcome_ref:
        raise EvidenceIntegrityError("clarification_resume_outcome_ref_invalid")
    if (
        str(outcome.get("source_run_id") or "") != source_run_id
        or str(outcome.get("thread_id") or "") != thread_id
        or str(outcome.get("topic_id") or "") != topic_id
    ):
        raise EvidenceIntegrityError("clarification_resume_outcome_owner_mismatch")
    if canonical_value(outcome.get("choice")) != canonical_value(dict(choice)):
        raise EvidenceIntegrityError("clarification_resume_choice_mismatch")
    outcome["outcome_signature"] = signature
    return {
        "source_run_id": source_run_id,
        "thread_id": thread_id,
        "topic_id": topic_id,
        "analysis_contract": typed_contract.to_dict(),
        "analysis_contract_signature": stored_contract_signature,
        "clarification_outcome": outcome,
    }


def _required(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise EvidenceIntegrityError(f"clarification_outcome_{field}_missing")
    return normalized
