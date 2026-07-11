from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any, Optional

from bi_agent.runtime.artifacts import to_jsonable
from bi_agent.runtime.authoritative_query_chain import (
    AuthoritativeQueryChainError,
    validate_authoritative_query_chain,
)
from bi_agent.runtime.clickhouse_query_compiler import (
    validate_clickhouse_query_contract,
)
from bi_agent.runtime.evidence_authority import (
    RowsPayloadLoader,
    RuntimeEvidenceResolver,
    canonical_digest,
    canonical_value,
    runtime_evidence_record_integrity_errors,
)
from bi_agent.runtime.runtime_contract_registry import (
    RuntimeContractRegistry,
    runtime_registry_integrity_error,
)
from bi_agent.runtime.dataset_catalog import DatasetReleaseResolver
from bi_agent.runtime.wording import wording_warnings
from bi_agent.runtime.claim_provenance import (
    build_context_manifest_record,
    build_trusted_claim_provenance_record,
    build_verified_claim_record,
    validate_context_manifest_record,
    validate_trusted_claim_provenance_record,
    validate_verified_claim_record,
)


def _verified_sources_from_claims(
    claims: Sequence[Mapping[str, Any]],
    evidence_by_ref: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    sources = []
    seen = set()
    for claim in claims:
        for evidence_ref in claim.get("evidence_refs") or ():
            ref = str(evidence_ref)
            evidence = evidence_by_ref.get(ref)
            if evidence is None:
                raise ValueError("verified_claim_evidence_missing")
            for source_type, source_ref in (
                ("evidence", ref),
                *(
                    ("completeness", str(completeness_ref))
                    for completeness_ref in evidence.get(
                        "completeness_record_refs"
                    )
                    or ()
                ),
            ):
                key = (source_type, source_ref)
                if key not in seen:
                    seen.add(key)
                    sources.append(
                        {
                            "type": source_type,
                            "ref": source_ref,
                            "can_support_claim": True,
                        }
                    )
    return tuple(sources)


def build_answer_package(
    *,
    run_id: str,
    draft_claims: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    evidence_resolver: Optional[RuntimeEvidenceResolver] = None,
    rows_loader: Optional[RowsPayloadLoader] = None,
    runtime_registry: Optional[RuntimeContractRegistry] = None,
    release_resolver: DatasetReleaseResolver | None = None,
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
    snapshot_id: str = "",
    permission_scope: str = "",
    analysis_contract: Optional[Mapping[str, Any]] = None,
    query_contracts: Sequence[Any] = (),
    query_results: Sequence[Any] = (),
    completeness_reports: Sequence[Any] = (),
    capability_execution_plans: Sequence[Any] = (),
    repair_attempts: Sequence[Any] = (),
    context_manifest: Optional[Mapping[str, Any]] = None,
    trusted_claim_provenance_records: Sequence[Mapping[str, Any]] = (),
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
        evidence_resolver=evidence_resolver,
        rows_loader=rows_loader,
        runtime_registry=runtime_registry,
        release_resolver=release_resolver,
        delivery_text={
            "answer_text": answer_text,
            "final_business_summary": final_business_summary,
            "final_explanation": final_explanation,
            "follow_up_questions": follow_up_questions,
            "semantic_audit": semantic_audit,
            "quality_gate": quality_gate,
        },
    )
    source_verifier_warnings = tuple(verifier.get("warnings") or ())
    accepted_claim_indexes = tuple(verifier.get("accepted_claim_indexes") or ())
    evidence_by_ref = {
        str(item.get("evidence_ref") or ""): item
        for item in evidence
        if item.get("evidence_ref")
    }
    factual_claims: tuple[dict[str, Any], ...] = ()
    projection_errors: list[dict[str, Any]] = []
    if accepted_claim_indexes:
        factual_claims, projection_errors = _authority_bound_claim_projections(
            claims=draft_claims,
            accepted_indexes=accepted_claim_indexes,
            evidence=evidence,
            evidence_resolver=evidence_resolver,
            rows_loader=rows_loader,
            runtime_registry=runtime_registry,
            release_resolver=release_resolver,
        )
        if len(factual_claims) != len(accepted_claim_indexes):
            projection_errors.append(
                {
                    "code": "verified_claim_projection_cardinality_mismatch",
                    "claim_index": -1,
                }
            )
    verified_manifest: dict[str, Any] = {}
    trusted_records: tuple[dict[str, Any], ...] = ()
    published_claims: tuple[dict[str, Any], ...] = ()
    if factual_claims and not projection_errors:
        try:
            context_owner = dict(context_manifest or {})
            sources = _verified_sources_from_claims(factual_claims, evidence_by_ref)
            verified_manifest = build_context_manifest_record(
                run_id=run_id,
                thread_id=str(
                    context_owner.get("thread_id")
                    or f"thread:runtime:{run_id}"
                ),
                topic_id=str(
                    context_owner.get("topic_id")
                    or f"topic:runtime:{run_id}"
                ),
                sources=sources,
                permission_context=context_owner.get("permission_context") or {},
            )
            supplied_context_ref = str(
                context_owner.get("manifest_id") or context_manifest_ref or ""
            )
            if supplied_context_ref and supplied_context_ref != verified_manifest["manifest_id"]:
                raise ValueError("context_manifest_ref_mismatch")
            if trusted_claim_provenance_records:
                if len(trusted_claim_provenance_records) != len(factual_claims):
                    raise ValueError("claim_provenance_cardinality_mismatch")
                trusted_records = tuple(
                    dict(to_jsonable(item))
                    for item in trusted_claim_provenance_records
                )
                for item in trusted_records:
                    validate_trusted_claim_provenance_record(item)
                    if item["run_id"] != run_id:
                        raise ValueError("claim_provenance_run_mismatch")
            else:
                trusted_records = tuple(
                    build_trusted_claim_provenance_record(run_id=run_id)
                    for _ in factual_claims
                )
            published_claims = tuple(
                build_verified_claim_record(
                    claim,
                    run_id=run_id,
                    context_manifest=verified_manifest,
                    evidence_by_ref=evidence_by_ref,
                    trusted_provenance=trusted,
                )
                for claim, trusted in zip(factual_claims, trusted_records)
            )
            projected_verifier = verify_answer_package(
                draft_claims=published_claims,
                evidence=evidence,
                visible_limitations=visible_limitations,
                evidence_resolver=evidence_resolver,
                rows_loader=rows_loader,
                runtime_registry=runtime_registry,
                release_resolver=release_resolver,
                delivery_text={
                    "answer_text": answer_text,
                    "final_business_summary": final_business_summary,
                    "final_explanation": final_explanation,
                    "follow_up_questions": follow_up_questions,
                    "semantic_audit": semantic_audit,
                    "quality_gate": quality_gate,
                },
            )
            if (
                projected_verifier.get("status")
                not in {"passed", "passed_with_warnings"}
                or len(projected_verifier.get("accepted_claim_indexes") or ())
                != len(published_claims)
            ):
                raise ValueError("verified_claim_reverification_failed")
            if source_verifier_warnings:
                projected_verifier = {
                    **projected_verifier,
                    "status": "passed_with_warnings",
                    "warnings": list(source_verifier_warnings),
                }
            verifier = projected_verifier
        except (TypeError, ValueError) as exc:
            projection_errors.append(
                {
                    "code": "verified_claim_provenance_invalid",
                    "claim_index": -1,
                    "reason": str(exc),
                }
            )
    if projection_errors:
        rejected = tuple(range(len(draft_claims)))
        verifier = {
            **verifier,
            "status": "failed",
            "errors": [*(verifier.get("errors") or ()), *projection_errors],
            "accepted_claim_indexes": (),
            "rejected_claim_indexes": rejected,
        }
        published_claims = ()
        verified_manifest = {}
        trusted_records = ()
    quality_gate = dict(to_jsonable(quality_gate))
    quality_gate["has_verified_claims"] = bool(published_claims)
    claim_groups = build_claim_groups(
        draft_claims=published_claims,
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
        "analysis_contract": canonical_value(analysis_contract or {}),
        "query_contracts": canonical_value(query_contracts),
        "query_results": canonical_value(query_results),
        "completeness_reports": canonical_value(completeness_reports),
        "capability_execution_plans": canonical_value(capability_execution_plans),
        "repair_attempts": canonical_value(repair_attempts),
        "context_manifest": verified_manifest,
        "verified_claims": canonical_value(published_claims),
        "trusted_claim_provenance_records": canonical_value(trusted_records),
    }

    package = {
        "run_id": run_id,
        "status": "draft",
        "package_type": "draft_answer_package",
        "snapshot_id": snapshot_id,
        "permission_scope": permission_scope,
        "context_manifest_ref": str(verified_manifest.get("manifest_id") or ""),
        "reuse_decisions": [],
        "final_answer": final_business_summary or answer_text,
        "follow_up_questions": list(follow_up_questions),
        "quality_gate": quality_gate,
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
                    "claims": to_jsonable(published_claims),
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
    return scrub_answer_package_for_delivery(
        package,
        retain_internal_audit=True,
    )


def scrub_answer_package_for_delivery(
    package: Mapping[str, Any],
    *,
    retain_internal_audit: bool = False,
) -> dict[str, Any]:
    scrubbed = dict(to_jsonable(package))
    admin = scrubbed.get("admin_audit")
    verifier = admin.get("verifier") if isinstance(admin, Mapping) else None
    verifier = verifier if isinstance(verifier, Mapping) else {}
    rejected = tuple(verifier.get("rejected_claim_indexes") or ())
    if verifier.get("status") in {"passed", "passed_with_warnings"} and not rejected:
        return scrubbed

    reasons = [
        {
            key: item[key]
            for key in ("code", "claim_index")
            if key in item
        }
        for item in (verifier.get("errors") or ())
        if isinstance(item, Mapping)
    ]
    if not reasons:
        reasons = [{"code": "evidence_verifier_not_passed"}]
    block = {
        "status": "blocked",
        "code": "evidence_verifier_failed",
        "reasons": reasons,
    }
    scrubbed["status"] = "failed"
    scrubbed["final_answer"] = ""
    scrubbed["answer_text"] = ""
    scrubbed["final_business_summary"] = ""
    scrubbed["follow_up_questions"] = []
    scrubbed["semantic_audit"] = _machine_audit_fields(
        scrubbed.get("semantic_audit")
    )
    scrubbed["final_explanation"] = block
    scrubbed["evidence_verifier_block"] = block
    quality_gate = scrubbed.get("quality_gate")
    safe_quality = _machine_audit_fields(quality_gate)
    safe_quality.update(
        {
            "status": "failed",
            "code": "evidence_verifier_failed",
            "has_verified_claims": False,
            "business_insight_present": False,
            "direct_answer": False,
            "blocks_display": True,
            "display_status": "failed",
        }
    )
    scrubbed["quality_gate"] = safe_quality
    if not retain_internal_audit:
        scrubbed["llm_calls"] = []
        scrubbed["admin_audit"] = {"verifier": to_jsonable(verifier)}
    for section in scrubbed.get("sections") or ():
        if not isinstance(section, dict):
            continue
        if not retain_internal_audit and (
            section.get("visibility") == "admin_audit" or section.get(
            "section_id", section.get("id")
            ) == "admin_audit"
        ):
            section["payload"] = {"verifier": to_jsonable(verifier)}
            continue
        if section.get("visibility") != "business_summary" and section.get(
            "section_id",
            section.get("id"),
        ) != "summary":
            continue
        payload = section.get("payload")
        if not isinstance(payload, dict):
            continue
        payload["answer_text"] = ""
        payload["final_business_summary"] = ""
        payload["claims"] = []
        payload["claim_groups"] = []
        payload["limitations"] = [
            item
            for item in (payload.get("limitations") or ())
            if _is_machine_code(item)
        ]
        payload["visualization_plan"] = {
            "status": "blocked",
            "code": "evidence_verifier_failed",
            "blocks": [],
        }
        payload["final_explanation"] = block
    return scrubbed


def reverify_answer_package_for_delivery(
    package: Mapping[str, Any],
    *,
    evidence_resolver: RuntimeEvidenceResolver | None,
    rows_loader: RowsPayloadLoader | None,
    runtime_registry: RuntimeContractRegistry | None,
    release_resolver: DatasetReleaseResolver | None = None,
    internal_verifier_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute delivery authority from persisted records at the Core boundary."""
    candidate = dict(to_jsonable(package))
    summary = _section_payload(candidate, "summary")
    evidence_section = _section_payload(candidate, "evidence")
    claims = tuple(
        item
        for item in summary.get("claims") or ()
        if isinstance(item, Mapping)
    )
    evidence = tuple(
        item
        for item in evidence_section.get("evidence") or ()
        if isinstance(item, Mapping)
    )
    recomputed = verify_answer_package(
        draft_claims=claims,
        evidence=evidence,
        visible_limitations=collect_visible_limitations(evidence),
        evidence_resolver=evidence_resolver,
        rows_loader=rows_loader,
        runtime_registry=runtime_registry,
        release_resolver=release_resolver,
        delivery_text={
            "final_answer": candidate.get("final_answer"),
            "answer_text": summary.get("answer_text"),
            "final_business_summary": summary.get("final_business_summary"),
            "final_explanation": candidate.get("final_explanation"),
            "follow_up_questions": candidate.get("follow_up_questions"),
            "semantic_audit": candidate.get("semantic_audit"),
            "quality_gate": candidate.get("quality_gate"),
        },
    )
    reported_admin = candidate.get("admin_audit")
    reported = (
        reported_admin.get("verifier")
        if isinstance(reported_admin, Mapping)
        else None
    )
    if _verifier_hard_partition(reported) != _verifier_hard_partition(recomputed):
        errors = list(recomputed.get("errors") or ())
        errors.append({"code": "reported_verifier_mismatch"})
        recomputed = {
            **recomputed,
            "status": "failed",
            "errors": errors,
        }
    elif (
        recomputed.get("status") in {"passed", "passed_with_warnings"}
        and isinstance(reported, Mapping)
        and reported.get("warnings")
    ):
        recomputed = {
            **recomputed,
            "status": "passed_with_warnings",
            "warnings": to_jsonable(reported.get("warnings") or ()),
        }
    projected_claims: tuple[dict[str, Any], ...] = ()
    if recomputed.get("status") in {"passed", "passed_with_warnings"}:
        projected_claims, projection_errors = _authority_bound_claim_projections(
            claims=claims,
            accepted_indexes=tuple(recomputed.get("accepted_claim_indexes") or ()),
            evidence=evidence,
            evidence_resolver=evidence_resolver,
            rows_loader=rows_loader,
            runtime_registry=runtime_registry,
            release_resolver=release_resolver,
        )
        accepted_indexes = tuple(recomputed.get("accepted_claim_indexes") or ())
        if len(projected_claims) != len(accepted_indexes):
            projection_errors.append(
                {
                    "code": "verified_claim_projection_cardinality_mismatch",
                    "claim_index": -1,
                }
            )
        if not projection_errors:
            try:
                context_record = dict(
                    reported_admin.get("context_manifest") or {}
                ) if isinstance(reported_admin, Mapping) else {}
                validate_context_manifest_record(context_record)
                provenance_records = tuple(
                    item
                    for item in (
                        reported_admin.get("trusted_claim_provenance_records")
                        or ()
                    )
                    if isinstance(item, Mapping)
                ) if isinstance(reported_admin, Mapping) else ()
                if len(provenance_records) != len(accepted_indexes):
                    raise ValueError("claim_provenance_cardinality_mismatch")
                for provenance in provenance_records:
                    validate_trusted_claim_provenance_record(provenance)
                evidence_by_ref = {
                    str(item.get("evidence_ref") or ""): item
                    for item in evidence
                    if item.get("evidence_ref")
                }
                rebuilt_claims = []
                for factual, provenance in zip(projected_claims, provenance_records):
                    rebuilt = build_verified_claim_record(
                        factual,
                        run_id=str(candidate.get("run_id") or ""),
                        context_manifest=context_record,
                        evidence_by_ref=evidence_by_ref,
                        trusted_provenance=provenance,
                    )
                    rebuilt_claims.append(rebuilt)
                projected_claims = tuple(rebuilt_claims)
            except (TypeError, ValueError) as exc:
                projection_errors.append(
                    {
                        "code": "verified_claim_provenance_invalid",
                        "claim_index": -1,
                        "reason": str(exc),
                    }
                )
        if projection_errors:
            rejected = tuple(
                sorted(
                    {
                        int(item["claim_index"])
                        for item in projection_errors
                        if "claim_index" in item
                    }
                )
            )
            recomputed = {
                **recomputed,
                "status": "failed",
                "errors": [*(recomputed.get("errors") or ()), *projection_errors],
                "accepted_claim_indexes": tuple(
                    index
                    for index in recomputed.get("accepted_claim_indexes") or ()
                    if index not in rejected
                ),
                "rejected_claim_indexes": tuple(
                    sorted(
                        set(recomputed.get("rejected_claim_indexes") or ())
                        | set(rejected)
                    )
                ),
            }
    if internal_verifier_audit is not None:
        internal_verifier_audit.clear()
        internal_verifier_audit.update(to_jsonable(recomputed))
    admin = dict(reported_admin) if isinstance(reported_admin, Mapping) else {}
    admin["verifier"] = recomputed
    candidate["admin_audit"] = admin
    for section in candidate.get("sections") or ():
        if not isinstance(section, dict):
            continue
        if section.get("visibility") == "admin_audit" or section.get(
            "section_id", section.get("id")
        ) == "admin_audit":
            payload = dict(section.get("payload") or {})
            payload["verifier"] = recomputed
            section["payload"] = payload
    return _project_client_answer_package(
        candidate,
        verifier=recomputed,
        claims=projected_claims,
        evidence=evidence,
        runtime_registry=runtime_registry,
    )


def _section_payload(package: Mapping[str, Any], section_id: str) -> dict[str, Any]:
    for section in package.get("sections") or ():
        if not isinstance(section, Mapping):
            continue
        if str(section.get("section_id") or section.get("id") or "") != section_id:
            continue
        payload = section.get("payload")
        return dict(payload) if isinstance(payload, Mapping) else {}
    return {}


def _verifier_partition(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    return to_jsonable(
        {
            key: value.get(key)
            for key in (
                "status",
                "errors",
                "warnings",
                "accepted_claim_indexes",
                "rejected_claim_indexes",
            )
        }
    )


def _verifier_hard_partition(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    status = str(value.get("status") or "")
    return to_jsonable(
        {
            "passed": status in {"passed", "passed_with_warnings"},
            "errors": value.get("errors"),
            "accepted_claim_indexes": value.get("accepted_claim_indexes"),
            "rejected_claim_indexes": value.get("rejected_claim_indexes"),
        }
    )


def _authority_bound_claim_projections(
    *,
    claims: Sequence[Mapping[str, Any]],
    accepted_indexes: Sequence[int],
    evidence: Sequence[Mapping[str, Any]],
    evidence_resolver: RuntimeEvidenceResolver | None,
    rows_loader: RowsPayloadLoader | None,
    runtime_registry: RuntimeContractRegistry | None,
    release_resolver: DatasetReleaseResolver | None,
) -> tuple[tuple[dict[str, Any], ...], list[dict[str, Any]]]:
    evidence_by_ref = {
        str(item.get("evidence_ref") or ""): item
        for item in evidence
        if item.get("evidence_ref")
    }
    projected = []
    errors = []
    for index in accepted_indexes:
        if type(index) is not int or index < 0 or index >= len(claims):
            errors.append(
                {
                    "code": "claim_factual_projection_failed",
                    "claim_index": index if type(index) is int else -1,
                    "reason": "accepted_claim_index_invalid",
                }
            )
            continue
        claim = claims[index]
        try:
            facts = _claim_authority_facts(
                claim,
                evidence_by_ref=evidence_by_ref,
                evidence_resolver=evidence_resolver,
                rows_loader=rows_loader,
                runtime_registry=runtime_registry,
                release_resolver=release_resolver,
            )
            claim_projection = _project_claim_from_authority(claim, facts)
            if claim_projection.get("fact_refs"):
                projected.append(claim_projection)
        except Exception as exc:
            errors.append(
                {
                    "code": "claim_factual_projection_failed",
                    "claim_index": index,
                    "reason": str(exc) or "authority_projection_failed",
                }
            )
    return tuple(projected), errors


@dataclass(frozen=True)
class CanonicalDimensionValue:
    value_type: str
    canonical_value: str
    display_value: str
    null_bucket: str


_MAX_DIMENSION_DECIMAL_DIGITS = 4096
_MAX_DIMENSION_DECIMAL_EXPONENT = 10000


@dataclass(frozen=True)
class AuthorityFact:
    fact_ref: str
    query_contract_ref: str
    result_ref: str
    metric_id: str
    value: Decimal
    window_id: str
    window_role: str
    observation_key: str
    dimensions: tuple[tuple[str, CanonicalDimensionValue], ...]
    grain: tuple[str, ...]
    value_semantics: str
    display_format: str

    @classmethod
    def create(
        cls,
        *,
        query_contract_ref: str,
        result_ref: str,
        metric_id: str,
        value: Decimal,
        window_id: str,
        window_role: str,
        observation_key: str,
        dimensions: tuple[tuple[str, CanonicalDimensionValue], ...],
        grain: tuple[str, ...],
        value_semantics: str = "raw_scalar",
        display_format: str = "number",
    ) -> "AuthorityFact":
        payload = {
            "query_contract_ref": query_contract_ref,
            "result_ref": result_ref,
            "metric_id": metric_id,
            "value": str(value),
            "window_id": window_id,
            "window_role": window_role,
            "observation_key": observation_key,
            "dimensions": dimensions,
            "grain": grain,
            "value_semantics": value_semantics,
            "display_format": display_format,
        }
        return cls(
            fact_ref=f"authority-fact:sha256:{canonical_digest(payload)}",
            query_contract_ref=query_contract_ref,
            result_ref=result_ref,
            metric_id=metric_id,
            value=value,
            window_id=window_id,
            window_role=window_role,
            observation_key=observation_key,
            dimensions=dimensions,
            grain=grain,
            value_semantics=value_semantics,
            display_format=display_format,
        )


def _claim_authority_facts(
    claim: Mapping[str, Any],
    *,
    evidence_by_ref: Mapping[str, Mapping[str, Any]],
    evidence_resolver: RuntimeEvidenceResolver | None,
    rows_loader: RowsPayloadLoader | None,
    runtime_registry: RuntimeContractRegistry | None,
    release_resolver: DatasetReleaseResolver | None,
) -> dict[str, Any]:
    if evidence_resolver is None or rows_loader is None or runtime_registry is None:
        raise ValueError("authority_projection_dependencies_missing")
    metric_ids: set[str] = set()
    grains: set[tuple[str, ...]] = set()
    target_windows: dict[str, dict[str, Any]] = {}
    baseline_windows: dict[str, dict[str, Any]] = {}
    authority_facts: list[AuthorityFact] = []
    authority_context_facts: list[dict[str, Any]] = []
    refs = tuple(str(ref) for ref in claim.get("evidence_refs") or ())
    if not refs:
        raise ValueError("authority_evidence_refs_missing")
    for ref in refs:
        evidence_item = evidence_by_ref.get(ref)
        if evidence_item is None:
            raise ValueError("authority_evidence_ref_missing")
        binding = evidence_resolver.resolve_capability_binding(
            str(evidence_item.get("binding_manifest_ref") or "")
        )
        if binding is None:
            raise ValueError("authority_binding_missing")
        chain = validate_authoritative_query_chain(
            binding,
            resolver=evidence_resolver,
            rows_loader=rows_loader,
            runtime_registry=runtime_registry,
            release_resolver=release_resolver,
        )
        if str(claim.get("claim_type") or "") not in binding.supported_claim_types:
            raise ValueError("authority_claim_type_mismatch")
        for result in chain.primary_results:
            query_record = chain.query_records[result.result_ref]
            contract = query_record.contract
            metric_bindings = {
                metric.metric_id: metric for metric in contract.metric_bindings
            }
            contract_metrics = tuple(metric_bindings)
            metric_ids.update(contract_metrics)
            grains.add(tuple(contract.result_shape.grain))
            for window in contract.resolved_windows:
                window_projection = {
                    "window_id": window.window_id,
                    "role": window.role,
                    "label": window.label,
                    "start_inclusive": window.start_inclusive,
                    "end_exclusive": window.end_exclusive,
                    "timezone": window.timezone,
                }
                if window.role == "target":
                    target_windows[window.window_id] = window_projection
                elif window.role == "baseline":
                    baseline_windows[window.window_id] = window_projection
            for row in result.rows:
                role = str(row.get("window_role") or "")
                window_id = str(row.get("window_id") or "")
                observation_key = str(row.get("observation_key") or "")
                dimensions = tuple(
                    sorted(
                        (
                            dimension.dimension_id,
                            _canonical_dimension_value(
                                row[dimension.source_field],
                                null_bucket=dimension.null_bucket,
                            ),
                        )
                        for dimension in contract.dimension_bindings
                    )
                )
                if contract.query_intent == "event_context_probe" and not str(
                    row.get("event_id") or ""
                ).startswith("__no_event__:"):
                    context_payload = {
                        "query_contract_ref": contract.query_contract_id,
                        "result_ref": result.result_ref,
                        "window_id": window_id,
                        "window_role": role,
                        "event_id": str(row.get("event_id") or ""),
                        "event_type": str(row.get("event_type") or ""),
                        "event_start_date": str(row.get("event_start_date") or ""),
                        "event_end_date": str(row.get("event_end_date") or ""),
                        "affected_scope": str(row.get("affected_scope") or ""),
                        "authority": str(row.get("authority") or ""),
                        "evidence_level": str(row.get("evidence_level") or ""),
                        "wording_limit": str(row.get("wording_limit") or ""),
                    }
                    if all(
                        context_payload[field]
                        for field in (
                            "event_id",
                            "event_type",
                            "event_start_date",
                            "event_end_date",
                            "affected_scope",
                            "authority",
                            "evidence_level",
                            "wording_limit",
                        )
                    ):
                        authority_context_facts.append(
                            {
                                **context_payload,
                                "fact_ref": (
                                    "authority-context-fact:sha256:"
                                    f"{canonical_digest(context_payload)}"
                                ),
                            }
                        )
                for metric_id in contract_metrics:
                    value = _decimal_value(row.get(metric_id))
                    if value is None:
                        continue
                    authority_facts.append(
                        AuthorityFact.create(
                            query_contract_ref=contract.query_contract_id,
                            result_ref=result.result_ref,
                            metric_id=metric_id,
                            value=value,
                            window_id=window_id,
                            window_role=role,
                            observation_key=observation_key,
                            dimensions=dimensions,
                            grain=tuple(contract.result_shape.grain),
                            value_semantics=metric_bindings[
                                metric_id
                            ].value_semantics,
                            display_format=metric_bindings[
                                metric_id
                            ].display_format,
                        )
                    )
    return {
        "metric_ids": tuple(sorted(metric_ids)),
        "authority_facts": tuple(authority_facts),
        "authority_context_facts": tuple(authority_context_facts),
        "grains": tuple(sorted(grains)),
        "target_windows": tuple(target_windows[key] for key in sorted(target_windows)),
        "baseline_windows": tuple(
            baseline_windows[key] for key in sorted(baseline_windows)
        ),
    }


def _project_claim_from_authority(
    claim: Mapping[str, Any],
    facts: Mapping[str, Any],
) -> dict[str, Any]:
    mappings = _map_claim_numbers_to_authority(claim, facts)
    context_facts = tuple(facts.get("authority_context_facts") or ())
    if not mappings and context_facts:
        return _project_context_claim_from_authority(claim, context_facts, facts)
    text = "".join(_render_authority_facts(mappings))
    projected = {
        "text": text,
        "claim_strength": str(claim.get("claim_strength") or ""),
        "claim_type": str(claim.get("claim_type") or ""),
        "evidence_refs": tuple(str(ref) for ref in claim.get("evidence_refs") or ()),
        "numbers": to_jsonable(
            {mapping["field"]: mapping["value"] for mapping in mappings}
        ),
        "fact_refs": [
            fact_ref
            for mapping in mappings
            for fact_ref in mapping["fact_refs"]
        ],
        "fact_selectors": {
            mapping["field"]: to_jsonable(mapping["fact_selector"])
            for mapping in mappings
        },
    }
    metrics = tuple(
        dict.fromkeys(mapping["metric_id"] for mapping in mappings)
    ) or tuple(facts.get("metric_ids") or ())
    if len(metrics) == 1:
        projected["target_metric"] = metrics[0]
    grains = tuple(facts.get("grains") or ())
    if len(grains) == 1:
        projected["grain"] = list(grains[0])
    selected_windows = {
        window_id
        for mapping in mappings
        for window_id in (
            mapping.get("window_id"),
            mapping.get("target_window_id"),
            mapping.get("baseline_window_id"),
        )
        if window_id
    }
    target_windows = tuple(
        window
        for window in facts.get("target_windows") or ()
        if not selected_windows or window["window_id"] in selected_windows
    )
    baseline_windows = tuple(
        window
        for window in facts.get("baseline_windows") or ()
        if not selected_windows or window["window_id"] in selected_windows
    )
    if len(target_windows) == 1:
        projected["target"] = dict(target_windows[0])
        projected["window"] = target_windows[0]["window_id"]
        projected["time_window"] = (
            f"{target_windows[0]['start_inclusive']}.."
            f"{target_windows[0]['end_exclusive']}"
        )
    if len(baseline_windows) == 1:
        projected["baseline"] = dict(baseline_windows[0])
    directions = tuple(
        dict.fromkeys(
            "positive"
            if mapping["value"] > 0
            else "negative" if mapping["value"] < 0 else "equal"
            for mapping in mappings
            if mapping["kind"] == "delta"
        )
    )
    if len(directions) == 1:
        projected["comparison_direction"] = directions[0]
    dimension_values: dict[str, set[CanonicalDimensionValue]] = {}
    for mapping in mappings:
        for dimension_id, dimension_value in mapping.get("dimensions") or ():
            dimension_values.setdefault(dimension_id, set()).add(dimension_value)
    if any(len(values) != 1 for values in dimension_values.values()):
        raise ValueError("claim_dimension_projection_ambiguous")
    if dimension_values:
        projected["dimensions"] = {
            dimension_id: _dimension_client_value(next(iter(values)))
            for dimension_id, values in sorted(dimension_values.items())
        }
    return projected


def _project_context_claim_from_authority(
    claim: Mapping[str, Any],
    context_facts: tuple[Mapping[str, Any], ...],
    facts: Mapping[str, Any],
) -> dict[str, Any]:
    lines = []
    for fact in context_facts:
        lines.append(
            "Window {window_id} overlaps event {event_type} "
            "({event_start_date}..{event_end_date}, {affected_scope}); "
            "authority={authority}, evidence_level={evidence_level}; "
            "this is candidate-mechanism context only.".format(**fact)
        )
    projected = {
        "text": " ".join(lines),
        "claim_strength": str(claim.get("claim_strength") or ""),
        "claim_type": str(claim.get("claim_type") or ""),
        "evidence_refs": tuple(str(ref) for ref in claim.get("evidence_refs") or ()),
        "numbers": {},
        "fact_refs": [str(fact["fact_ref"]) for fact in context_facts],
        "context_fact_selectors": [
            {
                "window_id": fact["window_id"],
                "event_id": fact["event_id"],
                "query_contract_ref": fact["query_contract_ref"],
                "result_ref": fact["result_ref"],
            }
            for fact in context_facts
        ],
    }
    target_windows = tuple(facts.get("target_windows") or ())
    if len(target_windows) == 1:
        projected["target"] = dict(target_windows[0])
        projected["window"] = target_windows[0]["window_id"]
        projected["time_window"] = (
            f"{target_windows[0]['start_inclusive']}.."
            f"{target_windows[0]['end_exclusive']}"
        )
    return projected


def _map_claim_numbers_to_authority(
    claim: Mapping[str, Any],
    facts: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    authority_facts = tuple(facts.get("authority_facts") or ())
    metric_ids = tuple(facts.get("metric_ids") or ())
    raw_dimensions = claim.get("dimensions") or {}
    if not isinstance(raw_dimensions, Mapping):
        raise ValueError("claim_dimensions_invalid")
    dimensions = {str(key): value for key, value in raw_dimensions.items()}
    numbers = claim.get("numbers") or {}
    if not isinstance(numbers, Mapping):
        raise ValueError("claim_numbers_invalid")
    mappings = []
    fact_selectors = claim.get("fact_selectors") or {}
    if not isinstance(fact_selectors, Mapping):
        raise ValueError("claim_fact_selectors_invalid")
    if any(not isinstance(field, str) for field in fact_selectors) or set(
        fact_selectors
    ) - {str(field) for field in numbers}:
        raise ValueError("claim_fact_selectors_unbound")
    claim_selector = _claim_level_fact_selector(claim)
    ordered_numbers = sorted(
        numbers.items(),
        key=lambda item: (
            0
            if str(item[0]).startswith("target_")
            else 1
            if str(item[0]).startswith("baseline_")
            else 2,
            str(item[0]),
        ),
    )
    for field, raw_value in ordered_numbers:
        value = _decimal_value(raw_value)
        if value is None:
            raise ValueError(f"claim_number_invalid:{field}")
        semantics = _number_field_semantics(str(field), metric_ids)
        if semantics is None:
            raise ValueError(f"claim_number_field_unbound:{field}")
        kind, metric_id, role = semantics
        has_field_selector = str(field) in fact_selectors
        raw_selector = fact_selectors.get(str(field), claim_selector)
        if not isinstance(raw_selector, Mapping):
            raise ValueError(f"claim_fact_selector_invalid:{field}")
        candidates = tuple(
            fact
            for fact in authority_facts
            if fact.metric_id == metric_id
            and (not role or fact.window_role == role)
            and (
                has_field_selector
                or _fact_dimensions_match(fact, dimensions)
            )
        )
        if kind == "delta":
            mapping = _map_delta_number(
                field=str(field),
                value=value,
                metric_id=metric_id,
                candidates=candidates,
                selector=raw_selector,
            )
        else:
            candidates = tuple(
                fact
                for fact in candidates
                if _fact_matches_selector(fact, raw_selector)
            )
            if len(candidates) != 1:
                raise ValueError(
                    f"claim_number_fact_not_unique:{field}:{len(candidates)}"
                )
            fact = candidates[0]
            if not _decimal_matches(value, {fact.value}):
                raise ValueError(f"claim_number_value_mismatch:{field}")
            mapping = {
                "field": str(field),
                "kind": "fact",
                "metric_id": metric_id,
                "value": fact.value,
                "fact_refs": (fact.fact_ref,),
                "window_id": fact.window_id,
                "window_role": fact.window_role,
                "observation_key": fact.observation_key,
                "dimensions": fact.dimensions,
                "grain": fact.grain,
                "value_semantics": fact.value_semantics,
                "display_format": fact.display_format,
                "fact_selector": _authority_fact_selector(fact),
            }
        mappings.append(mapping)
    return tuple(mappings)


def _number_field_semantics(
    field: str,
    metric_ids: Sequence[str],
) -> tuple[str, str, str] | None:
    lowered = field.lower()
    role = ""
    for prefix, candidate_role in (("target_", "target"), ("baseline_", "baseline")):
        if lowered.startswith(prefix):
            role = candidate_role
            lowered = lowered[len(prefix) :]
            break
    for metric_id in sorted(metric_ids, key=len, reverse=True):
        metric = metric_id.lower()
        if lowered in {metric, f"{metric}_value"}:
            return "fact", metric_id, role
        if lowered in {f"{metric}_delta", f"{metric}_change"}:
            return "delta", metric_id, ""
    if lowered in {"delta", "change", "difference", "uplift", "growth"} and len(
        metric_ids
    ) == 1:
        return "delta", metric_ids[0], ""
    return None


def _fact_dimensions_match(
    fact: AuthorityFact,
    expected: Mapping[str, Any],
) -> bool:
    actual = dict(fact.dimensions)
    for key, raw_value in expected.items():
        actual_value = actual.get(key)
        if actual_value is None:
            return False
        try:
            expected_value = _canonical_selector_dimension_value(
                raw_value,
                null_bucket=actual_value.null_bucket,
            )
        except (TypeError, ValueError):
            return False
        if actual_value != expected_value:
            return False
    return True


def _canonical_dimension_value(
    value: Any,
    *,
    null_bucket: str,
    declared_type: str = "",
) -> CanonicalDimensionValue:
    if not isinstance(null_bucket, str) or not null_bucket:
        raise ValueError("dimension_null_bucket_invalid")
    if declared_type == "null":
        if value != null_bucket:
            raise ValueError("dimension_null_selector_invalid")
        return CanonicalDimensionValue("null", null_bucket, null_bucket, null_bucket)
    if declared_type == "boolean":
        if type(value) is not bool:
            raise TypeError("dimension_boolean_selector_invalid")
    elif declared_type == "integer":
        if type(value) is not int:
            raise TypeError("dimension_integer_selector_invalid")
    elif declared_type == "number":
        if isinstance(value, bool) or not isinstance(
            value, (int, float, Decimal, str)
        ):
            raise TypeError("dimension_number_selector_invalid")
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("dimension_number_selector_invalid") from exc
        if not number.is_finite():
            raise ValueError("dimension_number_selector_invalid")
        canonical = _canonical_decimal_text(number)
        return CanonicalDimensionValue(
            "number", canonical, canonical, null_bucket
        )
    elif declared_type == "string":
        if type(value) is not str:
            raise TypeError("dimension_string_selector_invalid")
    elif declared_type:
        raise ValueError("dimension_selector_type_invalid")

    if value is None:
        return CanonicalDimensionValue("null", null_bucket, null_bucket, null_bucket)
    if type(value) is bool:
        canonical = "true" if value else "false"
        return CanonicalDimensionValue(
            "boolean", canonical, canonical, null_bucket
        )
    if type(value) is int:
        canonical = str(value)
        return CanonicalDimensionValue(
            "integer", canonical, canonical, null_bucket
        )
    if isinstance(value, (float, Decimal)):
        number = Decimal(str(value))
        if not number.is_finite() or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            raise ValueError("dimension_number_not_finite")
        canonical = _canonical_decimal_text(number)
        return CanonicalDimensionValue(
            "number", canonical, canonical, null_bucket
        )
    if type(value) is str:
        return CanonicalDimensionValue("string", value, value, null_bucket)
    raise TypeError(f"dimension_scalar_type_invalid:{type(value).__name__}")


def _canonical_selector_dimension_value(
    value: Any,
    *,
    null_bucket: str,
) -> CanonicalDimensionValue:
    if isinstance(value, Mapping):
        if set(value) == {"value_type", "value"}:
            return _canonical_dimension_value(
                value["value"],
                null_bucket=null_bucket,
                declared_type=str(value["value_type"]),
            )
        if set(value) == {
            "value_type",
            "canonical_value",
            "display_value",
        }:
            if value["value_type"] != "number" or any(
                type(value[field]) is not str
                for field in ("canonical_value", "display_value")
            ):
                raise ValueError("dimension_number_selector_invalid")
            canonical = _canonical_dimension_value(
                value["canonical_value"],
                null_bucket=null_bucket,
                declared_type="number",
            )
            if (
                canonical.canonical_value != value["canonical_value"]
                or canonical.display_value != value["display_value"]
            ):
                raise ValueError("dimension_number_selector_not_canonical")
            return canonical
        raise ValueError("dimension_selector_shape_invalid")
    return _canonical_dimension_value(value, null_bucket=null_bucket)


def _canonical_decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("dimension_number_not_finite")
    parts = value.as_tuple()
    digits = list(parts.digits)
    if len(digits) > _MAX_DIMENSION_DECIMAL_DIGITS:
        raise ValueError("dimension_number_precision_exceeded")
    while digits and digits[0] == 0:
        digits.pop(0)
    if not digits:
        return "0"
    exponent = int(parts.exponent)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    scientific_exponent = exponent + len(digits) - 1
    if abs(scientific_exponent) > _MAX_DIMENSION_DECIMAL_EXPONENT:
        raise ValueError("dimension_number_exponent_exceeded")
    coefficient = str(digits[0])
    if len(digits) > 1:
        coefficient += "." + "".join(str(digit) for digit in digits[1:])
    if scientific_exponent:
        coefficient += f"E{scientific_exponent:+d}"
    return f"-{coefficient}" if parts.sign else coefficient


def _dimension_client_value(value: CanonicalDimensionValue) -> Any:
    if value.value_type == "boolean":
        return value.canonical_value == "true"
    if value.value_type == "integer":
        return int(value.canonical_value)
    if value.value_type == "number":
        return value.display_value
    return value.display_value


def _dimension_selector_value(value: CanonicalDimensionValue) -> dict[str, Any]:
    if value.value_type == "number":
        return {
            "value_type": value.value_type,
            "canonical_value": value.canonical_value,
            "display_value": value.display_value,
        }
    return {
        "value_type": value.value_type,
        "value": _dimension_client_value(value),
    }


_FACT_SELECTOR_FIELDS = (
    "query_contract_ref",
    "result_ref",
    "metric_id",
    "metric",
    "window_role",
    "role",
    "window_id",
    "observation_key",
    "dimensions",
    "grain",
)


def _claim_level_fact_selector(claim: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: claim[field]
        for field in _FACT_SELECTOR_FIELDS
        if field in claim
    }


def _fact_matches_selector(
    fact: AuthorityFact,
    selector: Mapping[str, Any],
) -> bool:
    if set(selector) - set(_FACT_SELECTOR_FIELDS):
        return False
    metric_ids = {
        str(selector[field])
        for field in ("metric_id", "metric")
        if field in selector
    }
    roles = {
        str(selector[field])
        for field in ("window_role", "role")
        if field in selector
    }
    if len(metric_ids) > 1 or (metric_ids and fact.metric_id not in metric_ids):
        return False
    if len(roles) > 1 or (roles and fact.window_role not in roles):
        return False
    for field in (
        "query_contract_ref",
        "result_ref",
        "window_id",
        "observation_key",
    ):
        if field in selector and str(selector[field]) != str(getattr(fact, field)):
            return False
    if "dimensions" in selector:
        dimensions = selector["dimensions"]
        if not isinstance(dimensions, Mapping) or not _fact_dimensions_match(
            fact,
            {str(key): value for key, value in dimensions.items()},
        ):
            return False
    if "grain" in selector:
        grain = selector["grain"]
        if not isinstance(grain, Sequence) or isinstance(
            grain, (str, bytes, bytearray)
        ):
            return False
        if tuple(str(item) for item in grain) != fact.grain:
            return False
    return True


def _authority_fact_selector(fact: AuthorityFact) -> dict[str, Any]:
    return {
        "query_contract_ref": fact.query_contract_ref,
        "result_ref": fact.result_ref,
        "metric_id": fact.metric_id,
        "window_role": fact.window_role,
        "window_id": fact.window_id,
        "observation_key": fact.observation_key,
        "dimensions": {
            dimension_id: _dimension_selector_value(value)
            for dimension_id, value in fact.dimensions
        },
        "grain": list(fact.grain),
    }


def _map_delta_number(
    *,
    field: str,
    value: Decimal,
    metric_id: str,
    candidates: Sequence[AuthorityFact],
    selector: Mapping[str, Any],
) -> dict[str, Any]:
    shared_selector = {
        key: value
        for key, value in selector.items()
        if key not in {"target", "baseline"}
    }
    target_selector = selector.get("target") or {}
    baseline_selector = selector.get("baseline") or {}
    if not isinstance(target_selector, Mapping) or not isinstance(
        baseline_selector, Mapping
    ):
        raise ValueError(f"claim_delta_selector_invalid:{field}")
    targets = tuple(
        fact
        for fact in candidates
        if fact.window_role == "target"
        and _fact_matches_selector(fact, {**shared_selector, **target_selector})
    )
    baselines = tuple(
        fact
        for fact in candidates
        if fact.window_role == "baseline"
        and _fact_matches_selector(fact, {**shared_selector, **baseline_selector})
    )
    if len(targets) != 1 or len(baselines) != 1:
        raise ValueError(
            f"claim_delta_fact_not_unique:{field}:"
            f"target={len(targets)}:baseline={len(baselines)}"
        )
    target, baseline = targets[0], baselines[0]
    if (
        target.dimensions != baseline.dimensions
        or target.grain != baseline.grain
        or not target.observation_key
        or not baseline.observation_key
    ):
        raise ValueError(f"claim_delta_fact_incompatible:{field}")
    delta = target.value - baseline.value
    if not _decimal_matches(value, {delta}):
        raise ValueError(f"claim_delta_value_mismatch:{field}")
    if (
        target.value_semantics,
        target.display_format,
    ) != (
        baseline.value_semantics,
        baseline.display_format,
    ):
        raise ValueError(f"claim_delta_display_policy_mismatch:{field}")
    return {
        "field": field,
        "kind": "delta",
        "metric_id": metric_id,
        "value": delta,
        "fact_refs": (target.fact_ref, baseline.fact_ref),
        "window_id": target.window_id,
        "window_role": "comparison",
        "target_window_id": target.window_id,
        "baseline_window_id": baseline.window_id,
        "observation_key": target.observation_key,
        "dimensions": target.dimensions,
        "grain": target.grain,
        "value_semantics": target.value_semantics,
        "display_format": target.display_format,
        "fact_selector": {
            "metric_id": metric_id,
            "target": _authority_fact_selector(target),
            "baseline": _authority_fact_selector(baseline),
        },
    }


def _render_authority_facts(
    mappings: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    clauses = []
    for mapping in mappings:
        dimensions = "".join(
            f"，{key}={value.display_value}"
            for key, value in mapping.get("dimensions") or ()
        )
        metric_id = mapping["metric_id"]
        if mapping["kind"] == "delta":
            value = Decimal(mapping["value"])
            direction = "增加" if value > 0 else "减少" if value < 0 else "持平"
            clauses.append(
                f"目标期（{mapping['target_window_id']}）相较基线"
                f"（{mapping['baseline_window_id']}）{metric_id}{dimensions}"
                f"{direction}{_format_fact_value(abs(value), mapping)}。"
            )
            continue
        role_label = {
            "target": "目标期",
            "baseline": "基线期",
        }.get(mapping.get("window_role"), "分析窗口")
        observation = (
            f"，{mapping['observation_key']}" if mapping.get("observation_key") else ""
        )
        clauses.append(
            f"{role_label}（{mapping['window_id']}{observation}）"
            f"{metric_id}{dimensions}="
            f"{_format_fact_value(Decimal(mapping['value']), mapping)}。"
        )
    return tuple(dict.fromkeys(clauses))


def _format_fact_value(value: Decimal, policy: Mapping[str, Any]) -> str:
    as_percent = (
        policy.get("value_semantics") == "scalar_ratio"
        and policy.get("display_format") == "percent"
    )
    rendered = value * Decimal(100) if as_percent else value
    text = format(rendered, ",f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text}%" if as_percent else text


def _decimal_value(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _decimal_matches(value: Decimal, allowed: set[Decimal]) -> bool:
    return any(
        abs(value - candidate)
        <= max(Decimal("0.000000001"), abs(candidate) * Decimal("0.000000001"))
        for candidate in allowed
    )


def _evidence_strength_for_ref(
    evidence_ref: str,
    claims: Sequence[Mapping[str, Any]],
    registry: RuntimeContractRegistry | None,
) -> str:
    if registry is None:
        return "insufficient"
    strengths = tuple(
        str(claim.get("claim_strength") or "")
        for claim in claims
        if evidence_ref in tuple(str(ref) for ref in claim.get("evidence_refs") or ())
    )
    try:
        return max(strengths, key=registry.claim_strength_rank)
    except (KeyError, TypeError, ValueError):
        return "insufficient"


def _project_client_answer_package(
    candidate: Mapping[str, Any],
    *,
    verifier: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    runtime_registry: RuntimeContractRegistry | None,
) -> dict[str, Any]:
    passed = verifier.get("status") in {"passed", "passed_with_warnings"} and not (
        verifier.get("errors") or verifier.get("rejected_claim_indexes")
    )
    accepted_raw_claims = tuple(
        claim
        for claim in claims
    ) if passed else ()
    raw_accepted_refs = tuple(
        dict.fromkeys(
            ref
            for claim in accepted_raw_claims
            for ref in claim.get("evidence_refs") or ()
        )
    )
    evidence_ref_map = {
        str(ref): _safe_ref(ref) for ref in raw_accepted_refs
    }
    accepted_claims = tuple(
        _client_claim_projection(claim, evidence_ref_map=evidence_ref_map)
        for claim in accepted_raw_claims
    )
    accepted_refs = tuple(evidence_ref_map[str(ref)] for ref in raw_accepted_refs)
    evidence_by_ref = {
        str(item.get("evidence_ref") or ""): item
        for item in evidence
        if item.get("evidence_ref")
    }
    accepted_evidence = tuple(
        _client_evidence_projection(
            evidence_by_ref[str(ref)],
            evidence_ref=evidence_ref_map[str(ref)],
            strength=_evidence_strength_for_ref(
                str(ref),
                accepted_raw_claims,
                runtime_registry,
            ),
        )
        for ref in raw_accepted_refs
        if str(ref) in evidence_by_ref
    )
    client_verifier = _client_verifier_projection(verifier)
    final_answer = "\n".join(
        str(claim.get("text") or "").strip()
        for claim in accepted_claims
        if str(claim.get("text") or "").strip()
    )
    claim_groups = build_claim_groups(
        draft_claims=accepted_claims,
        evidence=accepted_evidence,
        verifier=client_verifier,
    )
    for claim, group in zip(accepted_claims, claim_groups):
        group["claim_id"] = claim["claim_id"]
    visualization = build_visualization_plan(claim_groups)
    for group, block in zip(claim_groups, visualization["blocks"]):
        block["claim_id"] = group["claim_id"]
    limitations = collect_visible_limitations(accepted_evidence)
    sql_hash = _safe_machine_string(_section_payload(candidate, "summary").get("sql_hash"))
    admin = {"verifier": client_verifier}
    status = "draft" if passed else "failed"
    quality_gate = {
        "status": verifier.get("status") if passed else "failed",
        "has_verified_claims": bool(accepted_claims),
        "business_insight_present": bool(final_answer),
        "blocks_display": not passed,
    }
    package = {
        "run_id": _safe_ref(candidate.get("run_id")),
        "status": status,
        "package_type": "draft_answer_package",
        "snapshot_id": _safe_ref(candidate.get("snapshot_id")),
        "permission_scope": _safe_ref(candidate.get("permission_scope")),
        "context_manifest_ref": _safe_ref(
            candidate.get("context_manifest_ref")
        ),
        "final_answer": final_answer,
        "follow_up_questions": [],
        "quality_gate": quality_gate,
        "proposed_graph": _safe_machine_strings(candidate.get("proposed_graph")),
        "accepted_graph": _safe_machine_strings(candidate.get("accepted_graph")),
        "rejected_or_degraded_mutations": [],
        "validator_results": [],
        "checkpoint_events": [],
        "llm_calls": [],
        "semantic_audit": {},
        "final_explanation": {},
        "delivery_claim_ids": [
            claim["claim_id"] for claim in accepted_claims
        ],
        "delivery_evidence_refs": list(accepted_refs),
        "sections": [
            {
                "section_id": "summary",
                "visibility": "business_summary",
                "payload": {
                    "answer_text": final_answer,
                    "final_business_summary": final_answer,
                    "claims": list(accepted_claims),
                    "claim_groups": claim_groups,
                    "visualization_plan": visualization,
                    "limitations": limitations,
                    "sql_hash": sql_hash,
                    "final_explanation": {},
                    "delivery_claim_ids": [
                        claim["claim_id"] for claim in accepted_claims
                    ],
                    "delivery_evidence_refs": list(accepted_refs),
                },
            },
            {
                "section_id": "evidence",
                "visibility": "aggregate_evidence",
                "payload": {
                    "evidence": list(accepted_evidence),
                    "sql_hash": sql_hash,
                },
            },
            {
                "section_id": "diagnostics",
                "visibility": "diagnostic_detail",
                "payload": {"sql_hash": sql_hash},
            },
            {
                "section_id": "admin_audit",
                "visibility": "admin_audit",
                "payload": admin,
            },
        ],
        "admin_audit": admin,
    }
    if not passed:
        package["evidence_verifier_block"] = {
            "status": "blocked",
            "code": "evidence_verifier_failed",
            "reasons": list(client_verifier.get("errors") or ()),
        }
    return package


def _client_claim_projection(
    claim: Mapping[str, Any],
    *,
    evidence_ref_map: Mapping[str, str],
) -> dict[str, Any]:
    projected = {
        key: to_jsonable(claim[key])
        for key in (
            "text",
            "claim_strength",
            "claim_type",
            "numbers",
            "scope",
            "time_window",
            "window",
            "grain",
            "comparison_direction",
            "dimensions",
            "fact_refs",
            "fact_selectors",
            "context_fact_selectors",
            "target_metric",
            "baseline",
            "target",
        )
        if key in claim
    }
    projected["evidence_refs"] = [
        evidence_ref_map[str(ref)]
        for ref in claim.get("evidence_refs") or ()
        if str(ref) in evidence_ref_map
    ]
    projected["claim_ref"] = _safe_ref(claim.get("claim_ref"))
    projected["context_manifest_ref"] = _safe_ref(
        claim.get("context_manifest_ref")
    )
    for field in (
        "result_refs",
        "completeness_record_refs",
        "artifact_refs",
        "memory_refs",
    ):
        projected[field] = [
            _safe_ref(ref) for ref in claim.get(field) or () if str(ref)
        ]
    projected["reuse_decisions"] = [
        {
            key: _safe_ref(item.get(key))
            for key in ("source_ref", "result_ref", "decision")
            if item.get(key)
        }
        for item in claim.get("reuse_decisions") or ()
        if isinstance(item, Mapping)
    ]
    projected["provenance_record_ref"] = _safe_ref(
        claim.get("provenance_record_ref")
    )
    projected["claim_id"] = f"claim:sha256:{canonical_digest(projected)}"
    return projected


def _client_evidence_projection(
    evidence: Mapping[str, Any],
    *,
    evidence_ref: str,
    strength: str,
) -> dict[str, Any]:
    projected = {
        key: to_jsonable(evidence[key])
        for key in (
            "evidence_type",
            "wording_limit",
            "analysis_contract_ref",
            "capability_contract_ref",
            "query_contract_refs",
            "result_refs",
            "query_execution_record_refs",
            "query_execution_record_digests",
            "rows_metadata_record_refs",
            "rows_metadata_record_digests",
            "completeness_report_refs",
            "completeness_record_refs",
            "completeness_record_digests",
            "source_snapshot_refs",
            "binding_manifest_ref",
            "binding_manifest_digest",
        )
        if key in evidence
    }
    projected["evidence_ref"] = evidence_ref
    projected["strength"] = strength
    for field in (
        "analysis_contract_ref",
        "capability_contract_ref",
        "binding_manifest_ref",
        "binding_manifest_digest",
    ):
        if field in projected:
            projected[field] = _safe_ref(projected[field])
    for field in (
        "query_contract_refs",
        "result_refs",
        "query_execution_record_refs",
        "query_execution_record_digests",
        "rows_metadata_record_refs",
        "rows_metadata_record_digests",
        "completeness_report_refs",
        "completeness_record_refs",
        "completeness_record_digests",
        "source_snapshot_refs",
    ):
        if field in projected:
            projected[field] = [_safe_ref(value) for value in projected[field]]
    projected["limitations"] = _safe_machine_strings(evidence.get("limitations"))
    return projected


def _client_verifier_projection(verifier: Mapping[str, Any]) -> dict[str, Any]:
    def issues(field: str) -> list[dict[str, Any]]:
        output = []
        for item in verifier.get(field) or ():
            if not isinstance(item, Mapping):
                continue
            code = _safe_machine_string(item.get("code"))
            if not code:
                continue
            issue = {"code": code}
            if type(item.get("claim_index")) is int:
                issue["claim_index"] = item["claim_index"]
            output.append(issue)
        return output

    return {
        "status": _safe_machine_string(verifier.get("status")) or "failed",
        "errors": issues("errors"),
        "warnings": issues("warnings"),
        "accepted_claim_indexes": [
            value
            for value in verifier.get("accepted_claim_indexes") or ()
            if type(value) is int and value >= 0
        ],
        "rejected_claim_indexes": [
            value
            for value in verifier.get("rejected_claim_indexes") or ()
            if type(value) is int and value >= 0
        ],
    }


def _safe_machine_string(value: Any) -> str:
    text = str(value or "")
    return text if _is_machine_code(text) else ""


def _safe_ref(value: Any) -> str:
    text = str(value or "")
    if text and all(
        char.isascii() and (char.isalnum() or char in "_.:@/-")
        for char in text
    ):
        return text
    return f"ref:sha256:{canonical_digest(text)}" if text else ""


def _safe_machine_strings(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [
        safe
        for item in value
        if (safe := _safe_machine_string(item))
    ]


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
        evidence_types = [_claim_metadata_value(item.get("evidence_type")) for item in ref_items]
        strengths = [_claim_metadata_value(item.get("strength")) for item in ref_items]
        wording_limits = [_claim_metadata_value(item.get("wording_limit")) for item in ref_items]
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
                "evidence_type": _claim_metadata_value(first.get("evidence_type")),
                "evidence_types": evidence_types,
                "strength": _claim_metadata_value(first.get("strength")),
                "strengths": strengths,
                "wording_limit": _claim_metadata_value(first.get("wording_limit")),
                "wording_limits": wording_limits,
                "limitations": limitations,
                "verifier_status": verifier.get("status"),
            }
        )
    return groups


def _claim_metadata_value(value: Any) -> str:
    text = str(value or "").strip()
    return text or "missing"


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
    evidence_resolver: RuntimeEvidenceResolver | None = None,
    rows_loader: RowsPayloadLoader | None = None,
    runtime_registry: RuntimeContractRegistry | None = None,
    release_resolver: DatasetReleaseResolver | None = None,
    delivery_text: Any = None,
) -> dict[str, Any]:
    evidence_by_ref = {item.get("evidence_ref"): item for item in evidence}
    errors = []

    for index, claim in enumerate(draft_claims):
        refs = tuple(claim.get("evidence_refs", ()))
        context_refs = tuple(claim.get("context_evidence_refs", ()))
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

        for ref in context_refs:
            if ref not in evidence_by_ref:
                errors.append(
                    {
                        "code": "missing_context_evidence_ref",
                        "claim_index": index,
                        "evidence_ref": ref,
                    }
                )
            elif _requires_authority(evidence_by_ref[ref]):
                errors.append(
                    {
                        "code": "authority_evidence_in_context_refs",
                        "claim_index": index,
                        "evidence_ref": ref,
                    }
                )
        if set(refs).intersection(context_refs):
            errors.append(
                {
                    "code": "evidence_ref_role_collision",
                    "claim_index": index,
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

        non_authority_refs = tuple(
            ref for ref in valid_refs if not _requires_authority(evidence_by_ref[ref])
        )
        if non_authority_refs:
            errors.append(
                {
                    "code": "context_evidence_in_publishable_refs",
                    "claim_index": index,
                    "evidence_refs": non_authority_refs,
                }
            )
        publishable_refs = tuple(ref for ref in valid_refs if ref not in non_authority_refs)
        authority_backed_refs = []
        provenance_errors = []
        for ref in publishable_refs:
            ref_errors = _claim_authority_errors(
                claim,
                (evidence_by_ref[ref],),
                evidence_resolver,
                rows_loader,
                runtime_registry,
                release_resolver,
            )
            if ref_errors:
                provenance_errors.extend(ref_errors)
                for code in (
                    "claim_strength_unknown",
                    "claim_strength_exceeds_authority",
                ):
                    if code in ref_errors:
                        errors.append(
                            {
                                "code": code,
                                "claim_index": index,
                            }
                        )
            else:
                authority_backed_refs.append(ref)
        if not authority_backed_refs:
            errors.append(
                {
                    "code": "claim_without_authority_backed_evidence",
                    "claim_index": index,
                }
            )
        if provenance_errors:
            errors.append(
                {
                    "code": "claim_missing_authoritative_provenance",
                    "claim_index": index,
                    "missing": tuple(dict.fromkeys(provenance_errors)),
                }
            )

        authority_projected = bool(
            claim.get("claim_ref")
            and (
                claim.get("fact_refs")
                or claim.get("context_fact_selectors")
            )
        )
        for key, expected in (() if authority_projected else claim.get("numbers", {}).items()):
            if not any(
                _numbers_match(
                    evidence_by_ref[ref].get("typed_payload", {}).get(key), expected
                )
                for ref in authority_backed_refs
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
            if authority_projected:
                continue
            expected = claim.get(field)
            if expected is None:
                continue
            seen = [
                evidence_by_ref[ref].get("typed_payload", {}).get(field)
                for ref in authority_backed_refs
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

    rejected_claim_indexes = tuple(
        sorted(
            {
                int(error["claim_index"])
                for error in errors
                if "claim_index" in error
            }
        )
    )
    accepted_claim_indexes = tuple(
        index
        for index in range(len(draft_claims))
        if index not in rejected_claim_indexes
    )
    if not accepted_claim_indexes and _contains_nonempty_text(delivery_text):
        errors.append({"code": "free_text_without_verified_claim"})

    warnings = wording_warnings(draft_claims, evidence_by_ref)
    rejected_claim_indexes = tuple(
        sorted(
            {
                int(error["claim_index"])
                for error in errors
                if "claim_index" in error
            }
        )
    )
    accepted_claim_indexes = tuple(
        index
        for index in range(len(draft_claims))
        if index not in rejected_claim_indexes
    )
    status = "failed" if errors else ("passed_with_warnings" if warnings else "passed")
    return {
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "accepted_claim_indexes": accepted_claim_indexes,
        "rejected_claim_indexes": rejected_claim_indexes,
    }


def collect_visible_limitations(evidence: Sequence[Mapping[str, Any]]) -> list[str]:
    limitations = []
    for item in evidence:
        for limitation in item.get("limitations", ()):
            if limitation not in limitations:
                limitations.append(limitation)
    return limitations


def _requires_authority(evidence: Mapping[str, Any]) -> bool:
    return str(evidence.get("evidence_type") or "") not in {
        "insufficient",
        "blocked",
        "context_only",
    }


def _claim_authority_errors(
    claim: Mapping[str, Any],
    evidence_items: Sequence[Mapping[str, Any]],
    resolver: RuntimeEvidenceResolver | None,
    rows_loader: RowsPayloadLoader | None,
    registry: RuntimeContractRegistry | None,
    release_resolver: DatasetReleaseResolver | None,
) -> tuple[str, ...]:
    if not evidence_items:
        return ("evidence",)
    required_refs = (
        "analysis_contract_ref",
        "capability_contract_ref",
        "query_contract_refs",
        "result_refs",
        "query_execution_record_refs",
        "query_execution_record_digests",
        "rows_metadata_record_refs",
        "rows_metadata_record_digests",
        "completeness_report_refs",
        "completeness_record_refs",
        "completeness_record_digests",
        "source_snapshot_refs",
        "supported_evidence_types",
        "supported_claim_types",
        "maximum_claim_strength",
        "maximum_claim_strength_rank",
        "claim_strength_taxonomy_version",
        "binding_manifest_ref",
        "binding_manifest_digest",
    )
    missing = []
    claim_type = str(claim.get("claim_type") or "")
    for evidence in evidence_items:
        missing.extend(
            field for field in required_refs if not evidence.get(field)
        )
        if evidence.get("input_status") != "ready":
            missing.append("input_status")
        completeness = tuple(evidence.get("input_completeness_statuses") or ())
        if not completeness or any(status != "complete" for status in completeness):
            missing.append("input_completeness_statuses")
        missing.extend(
            _authority_record_errors(
                evidence,
                resolver,
                rows_loader,
                claim_type,
                str(claim.get("claim_strength") or ""),
                registry,
                release_resolver,
            )
        )
    return tuple(dict.fromkeys(missing))


def _authority_record_errors(
    evidence: Mapping[str, Any],
    resolver: RuntimeEvidenceResolver | None,
    rows_loader: RowsPayloadLoader | None,
    claim_type: str,
    claim_strength: str,
    registry: RuntimeContractRegistry | None,
    release_resolver: DatasetReleaseResolver | None,
) -> tuple[str, ...]:
    missing = []
    if resolver is None:
        missing.append("runtime_evidence_resolver")
    if rows_loader is None:
        missing.append("rows_payload_loader")
    registry_error = runtime_registry_integrity_error(registry)
    if registry_error:
        missing.append(registry_error)
    if missing:
        return tuple(missing)
    try:
        return _resolved_authority_record_errors(
            evidence,
            resolver,
            rows_loader,
            claim_type,
            claim_strength,
            registry,
            release_resolver,
        )
    except Exception:
        return ("runtime_evidence_resolution_failed",)


def _resolved_authority_record_errors(
    evidence: Mapping[str, Any],
    resolver: RuntimeEvidenceResolver,
    rows_loader: RowsPayloadLoader,
    claim_type: str,
    claim_strength: str,
    registry: RuntimeContractRegistry,
    release_resolver: DatasetReleaseResolver | None,
) -> tuple[str, ...]:
    binding_ref = str(evidence.get("binding_manifest_ref") or "")
    binding = resolver.resolve_capability_binding(binding_ref)
    if binding is None:
        return ("capability_binding_record",)
    if runtime_evidence_record_integrity_errors(binding):
        return ("capability_binding_record_integrity",)
    errors = []
    try:
        validate_authoritative_query_chain(
            binding,
            resolver=resolver,
            rows_loader=rows_loader,
            runtime_registry=registry,
            release_resolver=release_resolver,
        )
    except AuthoritativeQueryChainError as exc:
        errors.append(f"authoritative_query_chain_invalid:{exc}")
    if binding.binding_digest != str(evidence.get("binding_manifest_digest") or ""):
        errors.append("binding_manifest_digest")
    scalar_fields = (
        (binding.capability_id, evidence.get("capability_id")),
        (binding.analysis_contract_ref, evidence.get("analysis_contract_ref")),
        (binding.status, evidence.get("input_status")),
        (
            binding.plan_payload.get("capability_contract_ref"),
            evidence.get("capability_contract_ref"),
        ),
    )
    if any(expected != actual for expected, actual in scalar_fields):
        errors.append("capability_binding_payload")
    if binding.status != "ready" or any(
        status != "complete" for status in binding.input_completeness_statuses
    ):
        errors.append("capability_binding_not_ready")
    evidence_type = str(evidence.get("evidence_type") or "")
    if tuple(evidence.get("supported_claim_types") or ()) != binding.supported_claim_types:
        errors.append("supported_claim_types_policy")
    if tuple(evidence.get("supported_evidence_types") or ()) != binding.supported_evidence_types:
        errors.append("supported_evidence_types_policy")
    if str(evidence.get("maximum_claim_strength") or "") != binding.maximum_claim_strength:
        errors.append("maximum_claim_strength_policy")
    if evidence.get("maximum_claim_strength_rank") != binding.maximum_claim_strength_rank:
        errors.append("maximum_claim_strength_rank_policy")
    if str(evidence.get("claim_strength_taxonomy_version") or "") != (
        binding.claim_strength_taxonomy_version
    ):
        errors.append("claim_strength_taxonomy_version_policy")
    try:
        expected_signature = registry.capability_contract_signature(
            binding.capability_id
        )
        if binding.capability_contract_signature != expected_signature:
            errors.append("capability_contract_signature_policy")
        if binding.capability_contract_version != registry.contract_version:
            errors.append("capability_contract_version_policy")
        capability = registry.capability_inputs(binding.capability_id)
        expected_maximum = str(capability.get("maximum_claim_strength") or "")
        expected_maximum_rank = registry.maximum_claim_strength_rank(
            expected_maximum
        )
        if binding.maximum_claim_strength != expected_maximum:
            errors.append("maximum_claim_strength_policy")
        if binding.maximum_claim_strength_rank != expected_maximum_rank:
            errors.append("maximum_claim_strength_rank_policy")
        if (
            binding.claim_strength_taxonomy_version
            != registry.claim_strength_taxonomy_version
        ):
            errors.append("claim_strength_taxonomy_version_policy")
        actual_rank = registry.claim_strength_rank(claim_strength)
    except (KeyError, TypeError, ValueError):
        errors.append("claim_strength_unknown")
    else:
        if actual_rank > binding.maximum_claim_strength_rank:
            errors.append("claim_strength_exceeds_authority")
    if not claim_type or claim_type not in binding.supported_claim_types:
        errors.append("supported_claim_type")
    if not evidence_type or evidence_type not in binding.supported_evidence_types:
        errors.append("supported_evidence_type")
    combined = (
        (
            (*binding.query_contract_refs, *binding.validation_query_contract_refs),
            evidence.get("query_contract_refs"),
        ),
        (
            (*binding.result_refs, *binding.validation_result_refs),
            evidence.get("result_refs"),
        ),
        (
            (
                *binding.query_execution_record_refs,
                *binding.validation_query_execution_record_refs,
            ),
            evidence.get("query_execution_record_refs"),
        ),
        (
            (
                *binding.query_execution_record_digests,
                *binding.validation_query_execution_record_digests,
            ),
            evidence.get("query_execution_record_digests"),
        ),
        (
            (
                *binding.rows_metadata_record_refs,
                *binding.validation_rows_metadata_record_refs,
            ),
            evidence.get("rows_metadata_record_refs"),
        ),
        (
            (
                *binding.rows_metadata_record_digests,
                *binding.validation_rows_metadata_record_digests,
            ),
            evidence.get("rows_metadata_record_digests"),
        ),
        (
            (
                *binding.completeness_report_refs,
                *binding.validation_completeness_report_refs,
            ),
            evidence.get("completeness_report_refs"),
        ),
        (
            (
                *binding.completeness_record_refs,
                *binding.validation_completeness_record_refs,
            ),
            evidence.get("completeness_record_refs"),
        ),
        (
            (
                *binding.completeness_record_digests,
                *binding.validation_completeness_record_digests,
            ),
            evidence.get("completeness_record_digests"),
        ),
        (
            (*binding.source_snapshot_refs, *binding.validation_source_snapshot_refs),
            evidence.get("source_snapshot_refs"),
        ),
    )
    for expected, actual in combined:
        if tuple(dict.fromkeys(expected)) != tuple(actual or ()):
            errors.append("capability_binding_refs")
    provenance_groups = (
        (
            binding.query_contract_refs,
            binding.result_refs,
            binding.query_execution_record_refs,
            binding.query_execution_record_digests,
            binding.rows_refs,
            binding.rows_metadata_record_refs,
            binding.rows_metadata_record_digests,
            binding.rows_content_hashes,
            binding.completeness_report_refs,
            binding.completeness_record_refs,
            binding.completeness_record_digests,
            set(binding.source_snapshot_refs),
        ),
        (
            binding.validation_query_contract_refs,
            binding.validation_result_refs,
            binding.validation_query_execution_record_refs,
            binding.validation_query_execution_record_digests,
            binding.validation_rows_refs,
            binding.validation_rows_metadata_record_refs,
            binding.validation_rows_metadata_record_digests,
            binding.validation_rows_content_hashes,
            binding.validation_completeness_report_refs,
            binding.validation_completeness_record_refs,
            binding.validation_completeness_record_digests,
            set(binding.validation_source_snapshot_refs),
        ),
    )
    for (
        query_refs,
        result_refs,
        query_record_refs,
        query_record_digests,
        rows_refs,
        rows_record_refs,
        rows_record_digests,
        row_hashes,
        report_refs,
        record_refs,
        record_digests,
        allowed_snapshot_refs,
    ) in provenance_groups:
        if not (
            len(query_refs)
            == len(result_refs)
            == len(query_record_refs)
            == len(query_record_digests)
            == len(rows_refs)
            == len(rows_record_refs)
            == len(rows_record_digests)
            == len(row_hashes)
            == len(report_refs)
            == len(record_refs)
            == len(record_digests)
        ):
            errors.append("capability_binding_ref_cardinality")
            continue
        for query_ref, result_ref, query_record_ref, query_record_digest, rows_ref, rows_record_ref, rows_record_digest, row_hash, report_ref, record_ref, record_digest in zip(
            query_refs,
            result_refs,
            query_record_refs,
            query_record_digests,
            rows_refs,
            rows_record_refs,
            rows_record_digests,
            row_hashes,
            report_refs,
            record_refs,
            record_digests,
        ):
            query = resolver.resolve_query_execution_record(query_record_ref)
            if (
                query is None
                or runtime_evidence_record_integrity_errors(query)
                or query.record_digest != query_record_digest
                or query.result_ref != result_ref
                or query.query_contract_ref != query_ref
                or query.rows_ref != rows_ref
                or query.completeness_report_ref != report_ref
                or not set(query.source_snapshot_refs).issubset(
                    allowed_snapshot_refs
                )
            ):
                errors.append("query_execution_record")
                continue
            rows = resolver.resolve_rows_record(rows_record_ref)
            if (
                rows is None
                or runtime_evidence_record_integrity_errors(rows)
                or rows.record_digest != rows_record_digest
                or rows.rows_ref != rows_ref
                or rows.rows_content_hash != row_hash
                or row_hash != query.rows_content_hash
            ):
                errors.append("rows_record")
            resolved_snapshots = {}
            snapshot_records_valid = True
            for snapshot_ref, snapshot_record_ref, snapshot_digest in zip(
                query.source_snapshot_refs,
                query.source_snapshot_record_refs,
                query.source_snapshot_record_digests,
            ):
                snapshot_record = resolver.resolve_snapshot(snapshot_ref)
                if (
                    snapshot_record is None
                    or runtime_evidence_record_integrity_errors(snapshot_record)
                    or snapshot_record.record_ref != snapshot_record_ref
                    or snapshot_record.record_digest != snapshot_digest
                ):
                    errors.append("snapshot_record_binding")
                    snapshot_records_valid = False
                    continue
                resolved_snapshots[snapshot_ref] = snapshot_record.snapshot
            if snapshot_records_valid:
                try:
                    validate_clickhouse_query_contract(
                        query.contract,
                        resolved_snapshots,
                        registry=registry,
                        release_resolver=release_resolver,
                    )
                except (PermissionError, TypeError, ValueError):
                    errors.append("query_contract_runtime_policy")
            report = resolver.resolve_completeness(record_ref)
            if (
                report is None
                or runtime_evidence_record_integrity_errors(report)
                or report.result_ref != result_ref
                or report.report_ref != report_ref
                or report.report_digest != record_digest
            ):
                errors.append("completeness_record")
                continue
            payload = report.report_payload
            if (
                payload.get("completeness_status") != "complete"
                or payload.get("analysis_readiness") != "ready"
                or payload.get("failure_reasons")
                or any(
                    assertion.get("passed") is not True
                    for assertion in payload.get("assertion_results") or ()
                )
            ):
                errors.append("completeness_record_not_ready")
    return tuple(dict.fromkeys(errors))


def _contains_nonempty_text(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_contains_nonempty_text(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return any(_contains_nonempty_text(item) for item in value)
    return False


def _is_machine_code(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value in {
        "blocked",
        "degraded",
        "failed",
        "invalid",
        "passed",
        "passed_with_warnings",
        "ready",
    }:
        return True
    return any(char in value for char in "_:.@/-") and all(
        char.isascii() and (char.isalnum() or char in "_:.@/-")
        for char in value
    )


def _machine_audit_fields(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = {}
    for key, item in value.items():
        key_text = str(key)
        if (
            key_text in {"status", "code", "codes", "issues", "warnings", "errors"}
            or key_text.endswith(("_ref", "_refs", "_id", "_ids", "_index", "_indexes"))
        ):
            normalized = _machine_audit_value(item)
            if normalized not in (None, "", [], {}):
                allowed[key_text] = normalized
        elif isinstance(item, (bool, int, float)) or item is None:
            allowed[key_text] = item
    return allowed


def _machine_audit_value(value: Any) -> Any:
    if isinstance(value, str):
        return value if _is_machine_code(value) else None
    if isinstance(value, Mapping):
        return _machine_audit_fields(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        output = []
        for item in value:
            normalized = _machine_audit_value(item)
            if normalized not in (None, "", [], {}):
                output.append(normalized)
        return output
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return None


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
