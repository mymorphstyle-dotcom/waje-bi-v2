from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from bi_agent.runtime.claim_authority import AuthorityBundle, RecommendationRecord
from bi_agent.runtime.claim_settlement import AuthorityBundleInputs
from bi_agent.runtime.evidence_authority import canonical_value
from bi_agent.runtime.narrative_workflow import NarrativeAnswerContext
from bi_agent.runtime.single_authority import IntentRevision


class NarrativeContextContractError(ValueError):
    pass


def _compact_json(value: Any) -> str:
    return json.dumps(
        canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _accepted_intent_context(intent: IntentRevision) -> Mapping[str, Any]:
    return {
        "goal_bindings": intent.goal_bindings,
        "target_metric_refs": intent.target_metric_refs,
        "scope": intent.scope,
        "time_spec": intent.time_spec,
        "comparison_spec": intent.comparison_spec,
        "direction_premise": intent.direction_premise,
        "requested_analysis_axes": intent.requested_analysis_axes,
        "requested_factor_refs": intent.requested_factor_refs,
        "desired_decisions": intent.desired_decisions,
    }


def _accepted_plan_context(authority_inputs: AuthorityBundleInputs) -> Mapping[str, Any]:
    plan = authority_inputs.execution_result.plan_revision
    tasks_by_id = {item.task_id: item for item in plan.capability_tasks}
    user_required_obligations = tuple(
        {
            "obligation_id": item.obligation_id,
            "claim_kind": item.claim_kind,
            "subject": item.subject,
            "minimum_claim_strength": item.success_policy["minimum_claim_strength"],
        }
        for item in plan.claim_obligations
        if item.role == "user_required"
    )
    analysis_axes = tuple(
        {
            "axis_id": item.axis_id,
            "role": item.role,
            "axis_kind": item.axis_kind,
            "target_metric_refs": item.target_metric_refs,
            "metric_refs": item.metric_refs,
            "dimension_refs": item.dimension_refs,
            "capability_refs": item.capability_refs,
            "reconciliation_group": item.reconciliation_group,
            "goal_refs": item.goal_refs,
            "supports_obligation_ids": item.supports_obligation_ids,
        }
        for item in plan.analysis_axes
    )
    capability_route = tuple(
        {
            "capability_id": item.capability_id,
            "execution_rank": item.execution_rank,
            "supports_obligation_ids": item.supports_obligation_ids,
            "depends_on_capability_ids": tuple(
                tasks_by_id[dependency_id].capability_id
                for dependency_id in item.dependency_task_ids
            ),
        }
        for item in sorted(
            plan.capability_tasks,
            key=lambda task: (task.execution_rank, task.task_id),
        )
    )
    return {
        "user_required_obligations": user_required_obligations,
        "analysis_axes": analysis_axes,
        "capability_route": capability_route,
    }


def build_narrative_answer_context(
    *,
    authority_bundle: AuthorityBundle,
    authority_inputs: AuthorityBundleInputs,
    intent_revision: IntentRevision,
    recommendations: Sequence[RecommendationRecord],
    locale: str,
    customer_term_labels: Mapping[str, str] | None = None,
    additional_business_context: Sequence[str] = (),
) -> NarrativeAnswerContext:
    if type(authority_bundle) is not AuthorityBundle:
        raise NarrativeContextContractError("narrative_context_bundle_invalid")
    if type(authority_inputs) is not AuthorityBundleInputs:
        raise NarrativeContextContractError("narrative_context_inputs_invalid")
    if type(intent_revision) is not IntentRevision:
        raise NarrativeContextContractError("narrative_context_intent_invalid")
    try:
        inputs = AuthorityBundleInputs.from_dict(authority_inputs.to_dict())
        bundle = AuthorityBundle.from_dict(
            authority_bundle.to_dict(),
            authority_inputs=inputs,
        )
        intent = IntentRevision.from_dict(intent_revision.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise NarrativeContextContractError(
            "narrative_context_authority_invalid"
        ) from exc
    normalized_recommendations = tuple(
        sorted(recommendations, key=lambda item: item.recommendation_ref)
    )
    if (
        bundle != authority_bundle
        or inputs != authority_inputs
        or intent != intent_revision
        or intent.intent_revision_id != inputs.intent_revision_id
        or intent.run_attempt_id != inputs.run_attempt_id
        or inputs.execution_result.plan_revision.intent_revision_id
        != intent.intent_revision_id
        or normalized_recommendations != inputs.recommendations
    ):
        raise NarrativeContextContractError("narrative_context_authority_invalid")

    context = [
        "accepted_scope=" + _compact_json(intent.scope),
        "accepted_time_spec=" + _compact_json(intent.time_spec),
        "target_metric_refs=" + _compact_json(intent.target_metric_refs),
        "desired_decisions=" + _compact_json(intent.desired_decisions),
        "active_decision_refs=" + _compact_json(bundle.decision_refs),
    ]
    labels = customer_term_labels or {}
    if not isinstance(labels, Mapping) or any(
        not isinstance(term, str)
        or not term
        or term != term.strip()
        or not isinstance(label, str)
        or not label
        or label != label.strip()
        for term, label in labels.items()
    ):
        raise NarrativeContextContractError(
            "narrative_context_customer_term_labels_invalid"
        )
    if labels:
        context.append(
            "customer_term_labels="
            + _compact_json(dict(sorted(labels.items())))
        )
    for item in additional_business_context:
        if not isinstance(item, str) or not item or item != item.strip():
            raise NarrativeContextContractError(
                "narrative_context_business_context_invalid"
            )
        context.append(item)
    return NarrativeAnswerContext.create(
        user_question=intent.original_user_text,
        answer_goal=(
            "Resolve the accepted business question using the accepted intent and "
            "plan context within the sealed claim and evidence ceilings."
        ),
        locale=locale,
        business_context=context,
        accepted_intent_context=_accepted_intent_context(intent),
        accepted_plan_context=_accepted_plan_context(inputs),
    )


__all__ = (
    "NarrativeContextContractError",
    "build_narrative_answer_context",
)
