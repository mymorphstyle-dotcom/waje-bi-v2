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
    goal_ids = tuple(
        str(item["goal_id"])
        for item in intent.goal_bindings
        if isinstance(item, Mapping) and item.get("goal_id")
    )
    return NarrativeAnswerContext.create(
        user_question=intent.original_user_text,
        answer_goal=(
            "Resolve the accepted analytical goals within the sealed claim and "
            "evidence ceilings: " + ", ".join(goal_ids)
        ),
        locale=locale,
        business_context=context,
    )


__all__ = (
    "NarrativeContextContractError",
    "build_narrative_answer_context",
)
