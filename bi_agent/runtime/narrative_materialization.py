from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping, Sequence

from bi_agent.runtime.authoritative_execution_result import (
    AuthoritativeExecutionResult,
)
from bi_agent.runtime.claim_authority import (
    AuthorityBundle,
    RecommendationRecord,
)
from bi_agent.runtime.claim_settlement import (
    AuthorityBundleInputs,
    ClaimSettlement,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.narrative_authority import PublicLimitation
from bi_agent.runtime.narrative_workflow import ReviewedPublicFactMaterialization
from bi_agent.runtime.public_fact_materialization import PublicFactMaterialization


class NarrativeMaterializationContractError(ValueError):
    pass


def _stable_records(
    values: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    by_digest: dict[str, Mapping[str, Any]] = {}
    for value in values:
        normalized = canonical_value(value)
        if not isinstance(normalized, Mapping) or not normalized:
            raise NarrativeMaterializationContractError(
                "public_limitation_context_record_invalid"
            )
        by_digest[canonical_digest(normalized)] = normalized
    return tuple(by_digest[key] for key in sorted(by_digest))


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    normalized = canonical_value(value)

    def freeze(item: Any) -> Any:
        if isinstance(item, Mapping):
            return MappingProxyType(
                {str(key): freeze(child) for key, child in item.items()}
            )
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    return freeze(normalized)


def _validated_authority(
    *,
    execution_result: AuthoritativeExecutionResult,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    recommendations: Sequence[RecommendationRecord],
) -> tuple[
    AuthoritativeExecutionResult,
    AuthorityBundle,
    ClaimSettlement,
    tuple[RecommendationRecord, ...],
]:
    if type(execution_result) is not AuthoritativeExecutionResult:
        raise NarrativeMaterializationContractError(
            "public_limitation_execution_invalid"
        )
    try:
        execution = AuthoritativeExecutionResult.from_dict(execution_result.to_dict())
        settlement = ClaimSettlement.from_dict(claim_settlement.to_dict())
        inputs = AuthorityBundleInputs.create(
            execution_result=execution,
            claim_settlement=settlement,
            recommendations=recommendations,
        )
        bundle = AuthorityBundle.from_dict(
            authority_bundle.to_dict(),
            authority_inputs=inputs,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise NarrativeMaterializationContractError(
            "public_limitation_authority_invalid"
        ) from exc
    if (
        execution != execution_result
        or settlement != claim_settlement
        or bundle != authority_bundle
        or tuple(inputs.recommendations)
        != tuple(sorted(recommendations, key=lambda item: item.recommendation_ref))
    ):
        raise NarrativeMaterializationContractError(
            "public_limitation_authority_invalid"
        )
    return execution, bundle, settlement, inputs.recommendations


def build_public_limitation_contexts(
    execution_result: AuthoritativeExecutionResult,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    recommendations: Sequence[RecommendationRecord],
) -> Mapping[str, Mapping[str, Any]]:
    execution, bundle, settlement, normalized_recommendations = _validated_authority(
        execution_result=execution_result,
        authority_bundle=authority_bundle,
        claim_settlement=claim_settlement,
        recommendations=recommendations,
    )
    claim_key_by_ref = {item.claim_key: item for item in settlement.accepted_claim_keys}
    obligation_by_id = {
        item.obligation_id: item for item in execution.plan_revision.claim_obligations
    }
    claim_key_by_claim_ref = {
        claim.claim_ref: claim_key_by_ref[claim.claim_key]
        for claim in settlement.accepted_claims
    }
    task_by_id = {
        task.task_id: task for task in execution.plan_revision.capability_tasks
    }
    contexts: dict[str, Mapping[str, Any]] = {}
    for limitation_ref in bundle.limitation_refs:
        affected_claim_kinds: set[str] = set()
        global_authority = False
        claim_records = []
        for claim in settlement.accepted_claims:
            if limitation_ref not in set(claim.limitation_refs):
                continue
            claim_key = claim_key_by_ref.get(claim.claim_key)
            if claim_key is None:
                raise NarrativeMaterializationContractError(
                    "public_limitation_claim_key_missing"
                )
            claim_records.append(
                {
                    "claim_class": claim.claim_class,
                    "claim_kind": claim_key.claim_kind,
                    "subject": claim_key.subject,
                    "scope": claim_key.scope,
                    "grain": claim_key.grain,
                    "dimension_path": claim_key.dimension_path,
                }
            )
            affected_claim_kinds.add(claim_key.claim_kind)

        obligation_records = []
        for coverage in settlement.obligation_coverage:
            if limitation_ref not in set(coverage.limitation_refs):
                continue
            obligation = obligation_by_id.get(coverage.obligation_id)
            if obligation is None:
                raise NarrativeMaterializationContractError(
                    "public_limitation_obligation_missing"
                )
            obligation_records.append(
                {
                    "obligation_id": coverage.obligation_id,
                    "status": coverage.status,
                    "claim_kind": obligation.claim_kind,
                    "role": obligation.role,
                }
            )
            affected_claim_kinds.add(obligation.claim_kind)

        evidence_records = []
        outcome_records = []
        failure_records = []
        for (
            _,
            outcome,
            evidence_entries,
            failures,
        ) in execution.capability_outcome_bundles:
            for evidence in evidence_entries:
                if limitation_ref not in set(evidence.limitation_refs):
                    continue
                evidence_records.append(
                    {
                        "evidence_kind": evidence.evidence_kind,
                        "data_contract_state": evidence.data_contract_state,
                        "evidence_strength": evidence.evidence_strength,
                        "maximum_claim_strength": evidence.maximum_claim_strength,
                        "scope": evidence.scope,
                        "window_count": len(evidence.window_refs),
                        "dimension_path": evidence.dimension_path,
                        "supported_claim_kinds": evidence.supported_claim_kinds,
                    }
                )
                affected_claim_kinds.update(evidence.supported_claim_kinds)
            if limitation_ref not in set(outcome.limitation_refs):
                continue
            task = task_by_id.get(outcome.task_id)
            if task is None:
                raise NarrativeMaterializationContractError(
                    "public_limitation_outcome_task_missing"
                )
            outcome_records.append(
                {
                    "capability_id": task.capability_id,
                    "status": outcome.status,
                    "retryability": outcome.retryability,
                    "affected_obligation_ids": outcome.affected_obligation_ids,
                }
            )
            for obligation_id in outcome.affected_obligation_ids:
                obligation = obligation_by_id.get(obligation_id)
                if obligation is None:
                    raise NarrativeMaterializationContractError(
                        "public_limitation_obligation_missing"
                    )
                affected_claim_kinds.add(obligation.claim_kind)
            for failure in failures:
                failure_records.append(
                    {
                        "layer": failure.layer,
                        "kind": failure.kind,
                        "scope": failure.scope,
                        "retryability": failure.retryability,
                        "user_actionable": failure.user_actionable,
                        "business_boundary": failure.business_boundary,
                    }
                )
                global_authority = global_authority or (
                    failure.integrity_level == "shared_authority"
                    or failure.scope in {"run", "plan_revision"}
                )

        recommendation_records = []
        for recommendation in normalized_recommendations:
            if limitation_ref not in set(recommendation.risk_refs):
                continue
            recommendation_records.append(
                {
                    "action": recommendation.action,
                    "applicable_conditions": recommendation.applicable_conditions,
                }
            )
            for claim_ref in recommendation.supporting_claim_refs:
                claim_key = claim_key_by_claim_ref.get(claim_ref)
                if claim_key is None:
                    raise NarrativeMaterializationContractError(
                        "public_limitation_recommendation_claim_missing"
                    )
                affected_claim_kinds.add(claim_key.claim_kind)
        if not global_authority and not affected_claim_kinds:
            raise NarrativeMaterializationContractError(
                "public_limitation_application_scope_missing"
            )
        applicability_records = (
            {
                "scope_effect": (
                    "global_authority" if global_authority else "local_claim_family"
                ),
                "affected_claim_kinds": tuple(sorted(affected_claim_kinds)),
            },
        )
        sections = {
            "identity": (
                {
                    "boundary_code": limitation_ref,
                },
            ),
            "applicability": applicability_records,
            "claims": _stable_records(claim_records),
            "obligations": _stable_records(obligation_records),
            "evidence": _stable_records(evidence_records),
            "outcomes": _stable_records(outcome_records),
            "failures": _stable_records(failure_records),
            "recommendations": _stable_records(recommendation_records),
        }
        if (
            ":evidence_state:context_only:" in limitation_ref
            or limitation_ref.startswith("limitation:context-only:")
        ):
            sections["business_semantics"] = (
                {
                    "source_availability": "available",
                    "evidence_role": "background_context",
                    "allowed_use": "background_and_candidate_localization",
                    "blocked_use": "direct_attribution_or_causal_conclusion",
                    "customer_wording_policy": (
                        "describe_role_limit_not_missing_data"
                    ),
                },
            )
        elif limitation_ref in {
            "window_reconciliation_incomplete",
            "window_reconciliation_threshold_exceeded",
        }:
            sections["business_semantics"] = (
                {
                    "source_availability": "available",
                    "evidence_role": "unreconciled_partition_context",
                    "allowed_use": "background_context_only",
                    "blocked_use": "partition_contribution_or_ranking",
                    "customer_wording_policy": (
                        "describe_window_reconciliation_limit_and_keep_other_results"
                    ),
                },
            )
        context = {name: records for name, records in sections.items() if records}
        if not context:
            raise NarrativeMaterializationContractError(
                "public_limitation_context_missing"
            )
        try:
            limitation = PublicLimitation.create(
                limitation_ref=limitation_ref,
                public_context=context,
            )
        except (TypeError, ValueError) as exc:
            raise NarrativeMaterializationContractError(
                "public_limitation_context_invalid"
            ) from exc
        contexts[limitation_ref] = limitation.public_context
    if set(contexts) != set(bundle.limitation_refs):
        raise NarrativeMaterializationContractError(
            "public_limitation_context_closure_invalid"
        )
    return MappingProxyType(
        {key: _frozen_mapping(contexts[key]) for key in sorted(contexts)}
    )


def build_reviewed_public_materialization(
    *,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    public_fact_materialization: PublicFactMaterialization,
    public_limitation_context_by_ref: Mapping[str, Mapping[str, Any]],
) -> ReviewedPublicFactMaterialization:
    if type(public_fact_materialization) is not PublicFactMaterialization:
        raise NarrativeMaterializationContractError(
            "reviewed_materialization_fact_source_invalid"
        )
    materialization = public_fact_materialization
    if (
        materialization.authority_bundle_ref != authority_bundle.bundle_ref
        or materialization.authority_bundle_digest != authority_bundle.bundle_digest
        or materialization.materialization_state not in {"ready", "boundary_only"}
        or materialization.claims_without_public_facts
    ):
        raise NarrativeMaterializationContractError(
            "reviewed_materialization_fact_source_incomplete"
        )
    expected_limitation_refs = set(authority_bundle.limitation_refs)
    if (
        not isinstance(public_limitation_context_by_ref, Mapping)
        or set(public_limitation_context_by_ref) != expected_limitation_refs
    ):
        raise NarrativeMaterializationContractError(
            "reviewed_materialization_limitation_contexts_invalid"
        )
    limitations = []
    for ref in sorted(expected_limitation_refs):
        try:
            limitations.append(
                PublicLimitation.create(
                    limitation_ref=ref,
                    public_context=public_limitation_context_by_ref[ref],
                )
            )
        except (TypeError, ValueError) as exc:
            raise NarrativeMaterializationContractError(
                "reviewed_materialization_limitation_contexts_invalid"
            ) from exc
    try:
        reviewed = ReviewedPublicFactMaterialization.create(
            authority_bundle=authority_bundle,
            claim_settlement=claim_settlement,
            review_ref=materialization.materialization_ref,
            reviewed_by="public-fact-contract-materializer:v2",
            public_facts=materialization.public_facts,
            public_limitations=tuple(limitations),
        )
    except (TypeError, ValueError) as exc:
        raise NarrativeMaterializationContractError(
            "reviewed_materialization_integrity_invalid"
        ) from exc
    return ReviewedPublicFactMaterialization.from_dict(
        canonical_value(reviewed.to_dict()),
        authority_bundle=authority_bundle,
        claim_settlement=claim_settlement,
    )


__all__ = (
    "NarrativeMaterializationContractError",
    "build_public_limitation_contexts",
    "build_reviewed_public_materialization",
)
