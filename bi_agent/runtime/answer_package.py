from collections.abc import Mapping, Sequence
from typing import Any

from bi_agent.runtime.artifacts import to_jsonable
from bi_agent.runtime.wording import wording_warnings


def build_answer_package(
    *,
    run_id: str,
    draft_claims: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    checkpoint_events: Sequence[Mapping[str, Any]],
    proposed_graph: Sequence[str],
    accepted_graph: Sequence[str],
    rejected_or_degraded_mutations: Sequence[Mapping[str, Any]],
    validator_results: Sequence[Mapping[str, Any]],
    sql_text: str,
    sql_hash: str,
    artifact_audit: Mapping[str, Any],
) -> dict[str, Any]:
    evidence = to_jsonable(evidence)
    visible_limitations = collect_visible_limitations(evidence)
    verifier = verify_answer_package(
        draft_claims=draft_claims,
        evidence=evidence,
        visible_limitations=visible_limitations,
    )
    ordinary_audit = {"sql_hash": sql_hash}
    admin_audit = {
        **ordinary_audit,
        "validator_results": to_jsonable(validator_results),
        "artifact_audit": to_jsonable(artifact_audit),
        "sql_text": sql_text,
        "verifier": verifier,
    }

    return {
        "run_id": run_id,
        "status": "draft",
        "package_type": "draft_answer_package",
        "proposed_graph": list(proposed_graph),
        "accepted_graph": list(accepted_graph),
        "rejected_or_degraded_mutations": to_jsonable(rejected_or_degraded_mutations),
        "validator_results": to_jsonable(validator_results),
        "checkpoint_events": to_jsonable(checkpoint_events),
        "sections": [
            {
                "section_id": "summary",
                "visibility": "business_summary",
                "payload": {
                    "claims": to_jsonable(draft_claims),
                    "limitations": visible_limitations,
                    "sql_hash": sql_hash,
                },
            },
            {
                "section_id": "evidence",
                "visibility": "aggregate_evidence",
                "payload": {
                    "evidence": evidence,
                    "sql_hash": sql_hash,
                },
            },
            {
                "section_id": "diagnostics",
                "visibility": "diagnostic_detail",
                "payload": ordinary_audit,
            },
            {
                "section_id": "admin_audit",
                "visibility": "admin_audit",
                "payload": admin_audit,
            },
        ],
        "admin_audit": admin_audit,
    }


def verify_answer_package(
    *,
    draft_claims: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    visible_limitations: Sequence[str],
) -> dict[str, Any]:
    evidence_by_ref = {item.get("evidence_ref"): item for item in evidence}
    errors = []

    for index, claim in enumerate(draft_claims):
        refs = tuple(claim.get("evidence_refs", ()))
        valid_refs = [ref for ref in refs if ref in evidence_by_ref]
        for ref in refs:
            if ref not in evidence_by_ref:
                errors.append(
                    {
                        "code": "missing_evidence_ref",
                        "claim_index": index,
                        "evidence_ref": ref,
                    }
                )

        for key, expected in claim.get("numbers", {}).items():
            if not any(
                _numbers_match(
                    evidence_by_ref[ref].get("typed_payload", {}).get(key), expected
                )
                for ref in valid_refs
            ):
                errors.append(
                    {
                        "code": "number_mismatch",
                        "claim_index": index,
                        "field": key,
                        "expected": expected,
                    }
                )

        for field in ("scope", "time_window", "window"):
            expected = claim.get(field)
            if expected is None:
                continue
            seen = [
                evidence_by_ref[ref].get("typed_payload", {}).get(field)
                for ref in valid_refs
                if field in evidence_by_ref[ref].get("typed_payload", {})
            ]
            if not seen or any(value != expected for value in seen):
                errors.append(
                    {
                        "code": f"{field}_mismatch",
                        "claim_index": index,
                        "expected": expected,
                    }
                )

    missing_visibility = [
        limitation
        for item in evidence
        for limitation in item.get("limitations", ())
        if _must_be_visible(limitation) and limitation not in visible_limitations
    ]
    if missing_visibility:
        errors.append(
            {
                "code": "missing_limitation_visibility",
                "limitations": sorted(set(missing_visibility)),
            }
        )

    warnings = wording_warnings(draft_claims, evidence_by_ref)
    status = "failed" if errors else ("passed_with_warnings" if warnings else "passed")
    return {
        "status": status,
        "errors": errors,
        "warnings": warnings,
    }


def collect_visible_limitations(evidence: Sequence[Mapping[str, Any]]) -> list[str]:
    limitations = []
    for item in evidence:
        for limitation in item.get("limitations", ()):
            if limitation not in limitations:
                limitations.append(limitation)
    return limitations


def _numbers_match(actual: Any, expected: Any) -> bool:
    if actual is None:
        return False
    try:
        return abs(float(actual) - float(expected)) < 0.000001
    except (TypeError, ValueError):
        return actual == expected


def _must_be_visible(limitation: str) -> bool:
    return "missing" in limitation or "coverage" in limitation or "contract" in limitation
