from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_value,
)


RUNTIME_PUBLICATION_RECORD_GROUPS = (
    "query_contracts",
    "query_execution_records",
    "rows_records",
    "snapshot_records",
    "completeness_records",
    "capability_binding_records",
    "evidence_manifests",
    "context_manifests",
    "trusted_provenance_records",
    "verified_claims",
    "claim_links",
    "repair_attempts",
)
RUNTIME_PUBLICATION_INDEX_SCHEMA_VERSION = "analysis-runtime-publication-index.v1"

_RECORD_REF_FIELDS = {
    "query_contracts": "query_contract_id",
    "query_execution_records": "record_ref",
    "rows_records": "record_ref",
    "snapshot_records": "record_ref",
    "completeness_records": "record_ref",
    "capability_binding_records": "record_ref",
    "evidence_manifests": "evidence_ref",
    "context_manifests": "manifest_id",
    "trusted_provenance_records": "record_ref",
    "verified_claims": "claim_ref",
    "repair_attempts": "attempt_ref",
}


def runtime_publication_record_ref(
    group: str,
    payload: Mapping[str, Any],
) -> str:
    if group not in RUNTIME_PUBLICATION_RECORD_GROUPS:
        raise EvidenceIntegrityError("runtime_publication_group_invalid")
    if group == "claim_links":
        claim_ref = str(payload.get("claim_ref") or "")
        evidence_ref = str(payload.get("evidence_ref") or "")
        ref = f"{claim_ref}\x1f{evidence_ref}" if claim_ref and evidence_ref else ""
    else:
        ref = str(payload.get(_RECORD_REF_FIELDS[group]) or "")
    if not ref:
        raise EvidenceIntegrityError("runtime_publication_record_ref_invalid")
    return ref


def runtime_publication_index(bundle: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(bundle, Mapping):
        raise EvidenceIntegrityError("runtime_publication_bundle_invalid")
    contract = bundle.get("analysis_contract")
    if not isinstance(contract, Mapping):
        raise EvidenceIntegrityError("runtime_publication_contract_invalid")
    contract_ref = str(contract.get("analysis_contract_id") or "")
    if not contract_ref:
        raise EvidenceIntegrityError("runtime_publication_contract_invalid")

    record_refs: dict[str, list[str]] = {}
    for group in RUNTIME_PUBLICATION_RECORD_GROUPS:
        raw_records = bundle.get(group)
        if (
            not isinstance(raw_records, Sequence)
            or isinstance(raw_records, (str, bytes, bytearray))
        ):
            raise EvidenceIntegrityError("runtime_publication_record_group_invalid")
        refs = []
        for raw in raw_records:
            payload = canonical_value(raw)
            if not isinstance(payload, Mapping):
                raise EvidenceIntegrityError(
                    "runtime_publication_record_group_invalid"
                )
            refs.append(runtime_publication_record_ref(group, payload))
        if len(refs) != len(set(refs)):
            raise EvidenceIntegrityError("runtime_publication_record_ref_ambiguous")
        record_refs[group] = refs
    return canonical_value(
        {
            "schema_version": RUNTIME_PUBLICATION_INDEX_SCHEMA_VERSION,
            "analysis_contract_id": contract_ref,
            "ordered_refs": record_refs,
        }
    )
