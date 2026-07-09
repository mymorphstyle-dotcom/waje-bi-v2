from collections.abc import Mapping, Sequence
from typing import Any, Optional

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
    llm_calls: Sequence[Mapping[str, Any]] = (),
    semantic_audit: Optional[Mapping[str, Any]] = None,
    final_explanation: Optional[Mapping[str, Any]] = None,
    answer_text: str = "",
    final_business_summary: str = "",
    coverage_interpretation: Optional[Mapping[str, Any]] = None,
    clarification_outcome: Optional[Mapping[str, Any]] = None,
    causal_audit: Optional[Mapping[str, Any]] = None,
    causal_evidence_dossier: Optional[Mapping[str, Any]] = None,
    context_manifest_ref: str = "",
    reuse_decisions: Sequence[Mapping[str, Any]] = (),
    quality_gate: Optional[Mapping[str, Any]] = None,
    follow_up_questions: Sequence[str] = (),
    compiler_runtime_plan: Optional[Mapping[str, Any]] = None,
    contract_gap_diagnostics: Optional[Sequence[Mapping[str, Any]]] = None,
    row_query_plan: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    evidence = to_jsonable(evidence)
    semantic_audit = {} if semantic_audit is None else semantic_audit
    final_explanation = {} if final_explanation is None else final_explanation
    coverage_interpretation = (
        {} if coverage_interpretation is None else coverage_interpretation
    )
    clarification_outcome = (
        {} if clarification_outcome is None else clarification_outcome
    )
    causal_audit = {} if causal_audit is None else causal_audit
    causal_evidence_dossier = (
        {} if causal_evidence_dossier is None else causal_evidence_dossier
    )
    quality_gate = {} if quality_gate is None else quality_gate
    compiler_runtime_plan = {} if compiler_runtime_plan is None else compiler_runtime_plan
    contract_gap_diagnostics = (
        () if contract_gap_diagnostics is None else contract_gap_diagnostics
    )
    row_query_plan = {} if row_query_plan is None else row_query_plan
    visible_limitations = collect_visible_limitations(evidence)
    verifier = verify_answer_package(
        draft_claims=draft_claims,
        evidence=evidence,
        visible_limitations=visible_limitations,
    )
    claim_groups = build_claim_groups(
        draft_claims=draft_claims,
        evidence=evidence,
        verifier=verifier,
    )
    visualization_plan = build_visualization_plan(claim_groups)
    ordinary_audit = {"sql_hash": sql_hash}
    admin_audit = {
        **ordinary_audit,
        "validator_results": to_jsonable(validator_results),
        "artifact_audit": to_jsonable(artifact_audit),
        "sql_text": sql_text,
        "verifier": verifier,
        "llm_calls": to_jsonable(llm_calls),
        "semantic_audit": to_jsonable(semantic_audit),
        "coverage_interpretation": to_jsonable(coverage_interpretation),
        "clarification_outcome": to_jsonable(clarification_outcome),
        "causal_audit": to_jsonable(causal_audit),
        "causal_evidence_dossier": to_jsonable(causal_evidence_dossier),
        "compiler_runtime_plan": to_jsonable(compiler_runtime_plan),
        "contract_gap_diagnostics": to_jsonable(contract_gap_diagnostics),
        "row_query_plan": to_jsonable(row_query_plan),
    }

    return {
        "run_id": run_id,
        "status": "draft",
        "package_type": "draft_answer_package",
        "context_manifest_ref": context_manifest_ref,
        "reuse_decisions": to_jsonable(reuse_decisions),
        "final_answer": final_business_summary or answer_text,
        "follow_up_questions": list(follow_up_questions),
        "quality_gate": to_jsonable(quality_gate),
        "proposed_graph": list(proposed_graph),
        "accepted_graph": list(accepted_graph),
        "rejected_or_degraded_mutations": to_jsonable(rejected_or_degraded_mutations),
        "validator_results": to_jsonable(validator_results),
        "checkpoint_events": to_jsonable(checkpoint_events),
        "llm_calls": to_jsonable(llm_calls),
        "semantic_audit": to_jsonable(semantic_audit),
        "final_explanation": to_jsonable(final_explanation),
        "sections": [
            {
                "section_id": "summary",
                "visibility": "business_summary",
                "payload": {
                    "answer_text": answer_text,
                    "final_business_summary": final_business_summary,
                    "claims": to_jsonable(draft_claims),
                    "claim_groups": claim_groups,
                    "visualization_plan": visualization_plan,
                    "limitations": visible_limitations,
                    "sql_hash": sql_hash,
                    "final_explanation": to_jsonable(final_explanation),
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


def build_claim_groups(
    *,
    draft_claims: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    verifier: Mapping[str, Any],
) -> list[dict[str, Any]]:
    evidence_by_ref = {item.get("evidence_ref"): item for item in evidence}
    groups = []
    for claim in draft_claims:
        refs = list(claim.get("evidence_refs", ()))
        ref_items = [evidence_by_ref[ref] for ref in refs if ref in evidence_by_ref]
        first = ref_items[0] if ref_items else {}
        limitations = []
        for item in ref_items:
            for limitation in item.get("limitations", ()):
                if limitation not in limitations:
                    limitations.append(limitation)
        groups.append(
            {
                "text": claim.get("text", ""),
                "scope": claim.get("scope"),
                "baseline": claim.get("baseline", {}),
                "target": claim.get("target", {}),
                "target_metric": claim.get("target_metric"),
                "time_window": claim.get("time_window"),
                "evidence_refs": refs,
                "evidence_type": first.get("evidence_type"),
                "strength": first.get("strength"),
                "wording_limit": first.get("wording_limit"),
                "limitations": limitations,
                "verifier_status": verifier.get("status"),
            }
        )
    return groups


def build_visualization_plan(claim_groups: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "status": "validated",
        "blocks": [
            {
                "id": f"visual-{index + 1}",
                "block_type": _visual_block_type(group.get("evidence_refs", ())),
                "title": _visual_title(group.get("evidence_refs", ())),
                "claim_text": group.get("text", ""),
                "target_metric": group.get("target_metric"),
                "scope": group.get("scope"),
                "time_window": group.get("time_window"),
                "evidence_refs": list(group.get("evidence_refs", ())),
                "limitations": list(group.get("limitations", ())),
                "verifier_status": group.get("verifier_status"),
            }
            for index, group in enumerate(claim_groups)
        ],
    }


def _visual_block_type(evidence_refs: Sequence[str]) -> str:
    joined = " ".join(str(ref) for ref in evidence_refs)
    if "segment" in joined or "driver" in joined or "joint_attribution" in joined:
        return "contribution_breakdown"
    if "pattern" in joined or "phase" in joined:
        return "phase_profile"
    if "outlier" in joined or "anomaly" in joined:
        return "anomaly_review"
    return "period_comparison"


def _visual_title(evidence_refs: Sequence[str]) -> str:
    return {
        "contribution_breakdown": "贡献拆解",
        "phase_profile": "阶段模式",
        "anomaly_review": "异常复核",
        "period_comparison": "期间对比",
    }[_visual_block_type(evidence_refs)]


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

        if claim.get("claim_strength") == "strong" and any(
            evidence_by_ref[ref].get("wording_limit")
            not in {"supported", "stable_pattern"}
            for ref in valid_refs
        ):
            errors.append(
                {
                    "code": "strong_claim_without_supported_wording",
                    "claim_index": index,
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
        delta = abs(float(actual) - float(expected))
        return delta < 0.000001 or delta < 0.005
    except (TypeError, ValueError):
        return actual == expected


def _must_be_visible(limitation: str) -> bool:
    return "missing" in limitation or "coverage" in limitation or "contract" in limitation
