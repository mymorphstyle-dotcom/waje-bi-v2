from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Sequence


@dataclass(frozen=True)
class QueryAuditRefs:
    result_ref: str
    rows_ref: str
    completeness_report_ref: str


def query_audit_identity(
    query_hash: str,
    contract_signature: str,
    snapshot_refs: Sequence[str],
) -> str:
    snapshot_payload = json.dumps(
        tuple(str(item) for item in snapshot_refs),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    snapshot_identity = hashlib.sha256(
        snapshot_payload.encode("utf-8")
    ).hexdigest()
    return ":".join(
        (
            query_hash or "uncompiled",
            contract_signature or "unsigned",
            snapshot_identity,
        )
    )


def query_audit_refs(
    query_hash: str,
    contract_signature: str,
    snapshot_refs: Sequence[str],
    *,
    query_contract_ref: str,
    execution_attempt_ref: str,
) -> QueryAuditRefs:
    content_identity = query_audit_identity(
        query_hash,
        contract_signature,
        snapshot_refs,
    )
    execution_payload = json.dumps(
        {
            "content_identity": content_identity,
            "execution_attempt_ref": execution_attempt_ref,
            "query_contract_ref": query_contract_ref,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    execution_identity = hashlib.sha256(
        execution_payload.encode("utf-8")
    ).hexdigest()
    return QueryAuditRefs(
        result_ref=f"result:{execution_identity}",
        rows_ref=f"rows:{content_identity}",
        completeness_report_ref=f"completeness:{execution_identity}",
    )


def query_rows_ref(
    query_hash: str,
    contract_signature: str,
    snapshot_refs: Sequence[str],
) -> str:
    return "rows:" + query_audit_identity(
        query_hash,
        contract_signature,
        snapshot_refs,
    )
