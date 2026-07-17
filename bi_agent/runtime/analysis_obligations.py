from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry


@dataclass(frozen=True)
class ObligationRequest:
    question_families: tuple[str, ...]
    diagnostic_tags: tuple[str, ...]
    target_metrics: tuple[str, ...]
    dimension_ids: tuple[str, ...]
    baselines: tuple[str, ...]
    context_sources: tuple[str, ...]
    analysis_axis_ids: tuple[str, ...]
    required_outcomes: tuple[str, ...]
    claim_types: tuple[str, ...]

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
            dimension_ids=tuple(requirements.get("dimension_ids") or ()),
            baselines=tuple(requirements.get("baselines") or ()),
            context_sources=tuple(requirements.get("context_sources") or ()),
            analysis_axis_ids=tuple(requirements.get("analysis_axis_ids") or ()),
            required_outcomes=tuple(requirements.get("required_outcomes") or ()),
            claim_types=tuple(requirements.get("claim_types") or ()),
        )


@dataclass(frozen=True)
class ObligationResolution:
    required_capabilities: tuple[str, ...]
    conditional_capabilities: tuple[str, ...]
    independent_capabilities: tuple[str, ...]
    minimum_publishable_evidence: tuple[str, ...]
    mutations: tuple[Mapping[str, str], ...]


@dataclass(frozen=True)
class PartitionedObligationResolution(ObligationResolution):
    applicable_diagnostic_tags: tuple[str, ...]
    rejected_diagnostic_tags: tuple[str, ...]


def _validate_obligation_target_metrics(
    request: ObligationRequest,
    registry: RuntimeContractRegistry,
) -> None:
    unknown_metrics = tuple(
        metric for metric in request.target_metrics if metric not in registry.metric_ids
    )
    if unknown_metrics:
        raise ValueError(
            f"unknown_obligation_target_metric:{','.join(dict.fromkeys(unknown_metrics))}"
        )


def resolve_analysis_obligations(
    request: ObligationRequest,
    registry: RuntimeContractRegistry,
) -> ObligationResolution:
    _validate_obligation_target_metrics(request, registry)
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
            if obligation_condition_matches(rule["condition"], request, registry):
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
        if obligation_condition_matches(contract["condition"], request, registry):
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


def resolve_partitioned_analysis_obligations(
    request: ObligationRequest,
    registry: RuntimeContractRegistry,
) -> PartitionedObligationResolution:
    """Resolve composite-family routes without weakening the strict resolver.

    Diagnostic applicability is partitioned by persisted question family. A tag
    contributes capabilities when at least one family supports it; tags that
    match no family are recorded as rejected mutations while every family's
    base obligations remain in the merged result.
    """
    _validate_obligation_target_metrics(request, registry)
    known_diagnostics = set(registry.diagnostic_obligation_ids)
    diagnostic_tags_by_family = {
        family: [] for family in request.question_families
    }
    applicable_tags: list[str] = []
    rejected_tags: list[str] = []
    rejected_mutations: list[Mapping[str, str]] = []
    for tag in dict.fromkeys(request.diagnostic_tags):
        if tag not in known_diagnostics:
            rejected_tags.append(tag)
            rejected_mutations.append(
                {
                    "action": "rejected",
                    "capability": tag,
                    "reason": "unknown_diagnostic_rejected",
                }
            )
            continue
        supported_families = set(
            registry.diagnostic_obligation(tag)["supported_question_families"]
        )
        matching_families = tuple(
            family
            for family in request.question_families
            if family in supported_families
        )
        if not matching_families:
            rejected_tags.append(tag)
            rejected_mutations.append(
                {
                    "action": "rejected",
                    "capability": tag,
                    "reason": "diagnostic_question_family_incompatible",
                }
            )
            continue
        applicable_tags.append(tag)
        for family in matching_families:
            diagnostic_tags_by_family[family].append(tag)

    required: list[str] = []
    conditional: list[str] = []
    independent: list[str] = []
    evidence: list[str] = []
    for family in request.question_families:
        family_resolution = resolve_analysis_obligations(
            ObligationRequest(
                question_families=(family,),
                diagnostic_tags=tuple(diagnostic_tags_by_family[family]),
                target_metrics=request.target_metrics,
                dimension_ids=request.dimension_ids,
                baselines=request.baselines,
                context_sources=request.context_sources,
                analysis_axis_ids=request.analysis_axis_ids,
                required_outcomes=request.required_outcomes,
                claim_types=request.claim_types,
            ),
            registry,
        )
        required.extend(family_resolution.required_capabilities)
        conditional.extend(family_resolution.conditional_capabilities)
        independent.extend(family_resolution.independent_capabilities)
        evidence.extend(family_resolution.minimum_publishable_evidence)

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
    mutations = (
        *rejected_mutations,
        *(
            {
                "action": "obligation_required",
                "capability": capability,
            }
            for capability in (*ordered_required, *ordered_conditional)
        ),
    )
    return PartitionedObligationResolution(
        required_capabilities=ordered_required,
        conditional_capabilities=ordered_conditional,
        independent_capabilities=ordered_independent,
        minimum_publishable_evidence=tuple(dict.fromkeys(evidence)),
        mutations=tuple(mutations),
        applicable_diagnostic_tags=tuple(applicable_tags),
        rejected_diagnostic_tags=tuple(rejected_tags),
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


def obligation_condition_matches(
    condition: str,
    request: ObligationRequest,
    registry: RuntimeContractRegistry,
) -> bool:
    compatible_event_sources = set(
        registry.obligation_condition_context_sources(
            "event_context_requested"
        )
    )
    matches = {
        "baselines_present": bool(request.baselines),
        "dimensions_present": "dimension_localization" in request.analysis_axis_ids,
        "multiple_dimensions_present": len(set(request.dimension_ids)) > 1,
        "components_present": "formula_tree" in request.analysis_axis_ids,
        "event_context_requested": bool(
            "business_context" in request.analysis_axis_ids
            and compatible_event_sources.intersection(request.context_sources)
        ),
        "anomaly_review_requested": (
            "external_shock_candidate_or_anomaly" in request.claim_types
        ),
        "trust_review_requested": bool(
            "evidence_boundaries" in request.required_outcomes
            or "contract_coverage_and_trust_boundary" in request.claim_types
        ),
    }
    try:
        return matches[condition]
    except KeyError as exc:
        raise ValueError(f"unknown_obligation_condition:{condition}") from exc
