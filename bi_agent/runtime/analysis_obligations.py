from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry


@dataclass(frozen=True)
class ObligationRequest:
    question_families: tuple[str, ...]
    diagnostic_tags: tuple[str, ...]
    target_metrics: tuple[str, ...]
    requested_dimensions: tuple[str, ...]
    baselines: tuple[str, ...]
    context_sources: tuple[str, ...]
    claim_intents: tuple[str, ...]

    @classmethod
    def from_intent(
        cls,
        *,
        question_family: str,
        question_families: Sequence[str],
        target_metric: str,
        bound_context: Mapping[str, Any],
    ) -> "ObligationRequest":
        requirements = bound_context.get("analysis_requirements") or {}
        families = tuple(dict.fromkeys((question_family, *question_families)))
        return cls(
            question_families=tuple(item for item in families if item),
            diagnostic_tags=tuple(requirements.get("diagnostic_tags") or ()),
            target_metrics=tuple(requirements.get("target_metrics") or (target_metric,)),
            requested_dimensions=tuple(requirements.get("requested_dimensions") or ()),
            baselines=tuple(requirements.get("baselines") or ()),
            context_sources=tuple(requirements.get("context_sources") or ()),
            claim_intents=tuple(requirements.get("claim_intents") or ()),
        )


@dataclass(frozen=True)
class ObligationResolution:
    required_capabilities: tuple[str, ...]
    conditional_capabilities: tuple[str, ...]
    independent_capabilities: tuple[str, ...]
    minimum_publishable_evidence: tuple[str, ...]
    mutations: tuple[Mapping[str, str], ...]


def resolve_analysis_obligations(
    request: ObligationRequest,
    registry: RuntimeContractRegistry,
) -> ObligationResolution:
    unknown_metrics = tuple(
        metric for metric in request.target_metrics if metric not in registry.metric_ids
    )
    if unknown_metrics:
        raise ValueError(
            f"unknown_obligation_target_metric:{','.join(dict.fromkeys(unknown_metrics))}"
        )
    required: list[str] = []
    conditional: list[str] = []
    independent: list[str] = []
    evidence: list[str] = []
    for family in request.question_families:
        contract = registry.question_family_obligation(family)
        required.extend(contract["required_capabilities"])
        independent.extend(contract["independent_capabilities"])
        evidence.extend(contract["minimum_publishable_evidence"])
        for rule in contract["conditional_rules"]:
            if obligation_condition_matches(rule["condition"], request):
                conditional.extend(rule["add"])
    for tag in request.diagnostic_tags:
        contract = registry.diagnostic_obligation(tag)
        if not set(request.question_families).issubset(
            set(contract["supported_question_families"])
        ):
            raise ValueError(
                f"diagnostic_question_family_incompatible:{tag}:"
                f"{','.join(request.question_families)}"
            )
        if obligation_condition_matches(contract["condition"], request):
            required.extend(contract["required_capabilities"])
    ordered_required = registry.order_capabilities(required)
    required_set = set(ordered_required)
    ordered_conditional = registry.order_capabilities(
        capability for capability in conditional if capability not in required_set
    )
    conditional_set = set(ordered_conditional)
    ordered_independent = registry.order_capabilities(
        capability
        for capability in independent
        if capability not in required_set and capability not in conditional_set
    )
    mutations = tuple(
        {"action": "obligation_required", "capability": capability}
        for capability in (*ordered_required, *ordered_conditional)
    )
    return ObligationResolution(
        required_capabilities=ordered_required,
        conditional_capabilities=ordered_conditional,
        independent_capabilities=ordered_independent,
        minimum_publishable_evidence=tuple(dict.fromkeys(evidence)),
        mutations=mutations,
    )


def capability_dataset_requirements(
    capabilities: Sequence[str],
    target_metrics: Sequence[str],
    registry: RuntimeContractRegistry,
) -> dict[str, tuple[str, ...]]:
    resolved: dict[str, tuple[str, ...]] = {}
    targets = tuple(dict.fromkeys(str(item) for item in target_metrics if item))
    for capability_id in dict.fromkeys(str(item) for item in capabilities if item):
        try:
            contract = registry.capability_inputs(capability_id)
        except KeyError:
            continue
        metric_mode = str(contract.get("metric_mode") or "")
        allowed_metrics = set(contract.get("allowed_metrics") or ())
        required_metrics = tuple(contract.get("required_metrics") or ())
        optional_metrics = set(contract.get("optional_metrics") or ())
        metrics = (
            tuple(metric for metric in targets if metric in allowed_metrics)
            if metric_mode == "requested"
            else tuple(
                dict.fromkeys(
                    (
                        *(metric for metric in required_metrics if metric in targets),
                        *(metric for metric in targets if metric in optional_metrics),
                    )
                )
            )
        )
        allowed_datasets = set(contract.get("allowed_datasets") or ())
        datasets: list[str] = []
        for metric_id in metrics:
            try:
                sources = tuple(registry.metric_sources(metric_id))
            except (KeyError, TypeError, ValueError):
                continue
            reviewed = tuple(
                dataset_id
                for dataset_id in sources
                if not allowed_datasets or dataset_id in allowed_datasets
            )
            if len(reviewed) == 1 or (
                reviewed
                and str(contract.get("source_selection") or "")
                == "all_required_datasets"
            ):
                datasets.extend(reviewed)
        if datasets:
            resolved[capability_id] = tuple(dict.fromkeys(datasets))
    return resolved


def obligation_condition_matches(condition: str, request: ObligationRequest) -> bool:
    matches = {
        "baselines_present": bool(request.baselines),
        "dimensions_present": bool(request.requested_dimensions),
        "multiple_dimensions_present": len(request.requested_dimensions) > 1,
        "components_present": "formula_component_contribution" in request.claim_intents,
        "event_context_requested": bool(request.context_sources),
        "anomaly_review_requested": "external_shock_candidate_or_anomaly" in request.claim_intents,
        "trust_review_requested": "contract_coverage_and_trust_boundary" in request.claim_intents,
    }
    try:
        return matches[condition]
    except KeyError as exc:
        raise ValueError(f"unknown_obligation_condition:{condition}") from exc
