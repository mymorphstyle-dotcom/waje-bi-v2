from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from bi_agent.runtime.analysis_contracts import ContractGap


class SourceContractRegistry(Protocol):
    def metric_sources(self, metric_id: str) -> Mapping[str, Mapping[str, Any]]: ...

    def dimension_sources(
        self,
        dimension_id: str,
    ) -> Mapping[str, Mapping[str, Any]]: ...

    def capability_inputs(self, capability_id: str) -> Mapping[str, Any]: ...


def is_canonical_unsupported_required_claim_gap(gap: ContractGap) -> bool:
    """Recognize the compiler-owned terminal gap for a required claim."""
    if len(gap.affected_claim_types) != 1:
        return False
    claim_type = gap.affected_claim_types[0]
    diagnostic = gap.diagnostic_context
    return bool(
        claim_type != "unbound_claim_intent"
        and gap.gap_type == "contract_partial"
        and gap.gap_id == f"claim_intent:{claim_type}:unsupported"
        and gap.dataset_id == ""
        and gap.affected_capabilities == ("analysis_contract",)
        and gap.owner == "contract_owner"
        and gap.repair_options
        == ("add_supporting_capability", "report_unavailable_claim")
        and gap.requires_clarification is False
        and isinstance(diagnostic, Mapping)
        and dict(diagnostic)
        == {
            "claim_origin": "user_required",
            "publication_status": "unavailable",
        }
    )


def is_canonical_unbound_claim_intent_gap(
    gap: ContractGap,
    *,
    expected_capabilities: Sequence[str],
) -> bool:
    """Recognize the compiler-owned blocking gap for an unbound claim intent."""
    expected = tuple(expected_capabilities)
    affected = tuple(gap.affected_capabilities)
    return bool(
        gap.gap_type == "contract_partial"
        and gap.gap_id == "claim_intents:unbound"
        and gap.dataset_id == ""
        and affected
        and len(affected) == len(set(affected))
        and set(affected).issubset(expected)
        and gap.affected_claim_types == ("unbound_claim_intent",)
        and gap.owner == "contract_owner"
        and gap.repair_options
        == (
            "bind_capability_claim_types",
            "bind_metric_claim_types",
            "clarify_claim_intent",
        )
        and gap.requires_clarification is True
        and isinstance(gap.diagnostic_context, Mapping)
        and not gap.diagnostic_context
    )


def canonical_source_ambiguity_subset(
    registered_source_ids: Sequence[str],
    selected_source_ids: Sequence[str],
) -> tuple[str, ...]:
    """Return an exact registry-ordered ambiguity subset, or no authority."""
    registered = tuple(registered_source_ids)
    selected = tuple(selected_source_ids)
    if (
        len(registered) != len(set(registered))
        or any(not isinstance(item, str) or not item for item in registered)
        or len(selected) < 2
        or len(selected) != len(set(selected))
        or any(not isinstance(item, str) or not item for item in selected)
    ):
        return ()
    selected_set = set(selected)
    canonical = tuple(
        source_id for source_id in registered if source_id in selected_set
    )
    if canonical != selected:
        return ()
    return canonical


def canonical_source_ambiguity_source_ids(
    gap: ContractGap,
    *,
    registry: SourceContractRegistry,
) -> tuple[str, ...]:
    """Resolve the exact registry-backed source ids encoded by an ambiguity gap."""
    diagnostic = gap.diagnostic_context
    if not isinstance(diagnostic, Mapping):
        return ()
    item_kind = diagnostic.get("item_kind")
    item_id = diagnostic.get("item_id")
    if (
        item_kind not in {"metric", "dimension"}
        or not isinstance(item_id, str)
        or not item_id
    ):
        return ()
    try:
        registered = tuple(
            registry.metric_sources(item_id)
            if item_kind == "metric"
            else registry.dimension_sources(item_id)
        )
    except (KeyError, TypeError, ValueError):
        return ()
    prefix = f"{item_kind}:{item_id}:source_ambiguous:"
    if not gap.gap_id.startswith(prefix):
        return ()
    suffix = gap.gap_id[len(prefix):]
    selected = tuple(suffix.split(",")) if suffix else ()
    return canonical_source_ambiguity_subset(registered, selected)


def is_canonical_direct_analysis_source_ambiguity(
    gap: ContractGap,
    capabilities: tuple[str, ...],
    *,
    registry: SourceContractRegistry,
    expected_capability_requirements: Sequence[str] | None = None,
) -> bool:
    """Validate the exact direct-analysis source-ambiguity gap contract."""
    if not _is_direct_analysis_source_ambiguity_shape(
        gap,
        capabilities,
        registry=registry,
    ):
        return False
    claim_intents = gap.diagnostic_context.get("claim_intents")
    affected_claim_types = gap.affected_claim_types
    return bool(
        affected_claim_types
        and len(affected_claim_types) == len(set(affected_claim_types))
        and isinstance(claim_intents, (list, tuple))
        and tuple(claim_intents) == affected_claim_types
        and _is_canonical_final_gap_scope(
            gap,
            capabilities,
            affected_claim_types=affected_claim_types,
            registry=registry,
            expected_capability_requirements=expected_capability_requirements,
        )
    )


def is_unscoped_direct_analysis_source_ambiguity(
    gap: ContractGap,
    capabilities: tuple[str, ...],
    *,
    registry: SourceContractRegistry,
) -> bool:
    """Recognize the exact compiler-internal shape before claim scoping."""
    if not _is_direct_analysis_source_ambiguity_shape(
        gap,
        capabilities,
        registry=registry,
    ):
        return False
    claim_intents = gap.diagnostic_context.get("claim_intents")
    return (
        tuple(gap.affected_capabilities) == capabilities
        and capabilities == ("analysis_contract",)
        and not gap.affected_claim_types
        and isinstance(claim_intents, (list, tuple))
        and not claim_intents
    )


def _is_direct_analysis_source_ambiguity_shape(
    gap: ContractGap,
    capabilities: tuple[str, ...],
    *,
    registry: SourceContractRegistry,
) -> bool:
    diagnostic = gap.diagnostic_context
    if not isinstance(diagnostic, Mapping) or set(diagnostic) != {
        "item_kind",
        "item_id",
        "claim_intents",
    }:
        return False
    return bool(
        canonical_source_ambiguity_source_ids(gap, registry=registry)
        and gap.gap_type == "contract_partial"
        and gap.dataset_id == ""
        and gap.owner == "contract_owner"
        and gap.repair_options
        == ("select_dataset_requirement", "clarify_source_scope")
        and gap.requires_clarification is True
    )


def reviewed_queryless_gap_claim_types(
    capability_id: str,
    affected_claim_types: Sequence[str],
    *,
    registry: SourceContractRegistry,
) -> tuple[str, ...]:
    """Return the gap claims a reviewed queryless capability can authorize."""
    affected = tuple(affected_claim_types)
    if (
        not isinstance(capability_id, str)
        or not capability_id
        or not affected
        or len(affected) != len(set(affected))
        or any(not isinstance(item, str) or not item for item in affected)
    ):
        return ()
    try:
        contract = registry.capability_inputs(capability_id)
    except (KeyError, TypeError, ValueError):
        return ()
    if not isinstance(contract, Mapping):
        return ()
    query_families = contract.get("query_families")
    required_metrics = contract.get("required_metrics")
    readiness = contract.get("minimum_readiness")
    degradation = contract.get("degradation_policy")
    supported = contract.get("supported_claim_types")
    if (
        not isinstance(query_families, (list, tuple))
        or query_families
        or not isinstance(required_metrics, (list, tuple))
        or required_metrics
        or not isinstance(readiness, Mapping)
        or readiness.get("required_slots") != "none"
        or not isinstance(degradation, Mapping)
        or not str(degradation.get("missing_required_input") or "").startswith(
            "block_"
        )
        or not isinstance(supported, (list, tuple))
        or not supported
        or len(supported) != len(set(supported))
        or any(not isinstance(item, str) or not item for item in supported)
    ):
        return ()
    supported_set = set(supported)
    if not set(affected).issubset(supported_set):
        return ()
    return affected


def _is_canonical_final_gap_scope(
    gap: ContractGap,
    capabilities: tuple[str, ...],
    *,
    affected_claim_types: tuple[str, ...],
    registry: SourceContractRegistry,
    expected_capability_requirements: Sequence[str] | None,
) -> bool:
    if (
        tuple(gap.affected_capabilities) != capabilities
        or not capabilities
        or len(capabilities) != len(set(capabilities))
        or capabilities[0] != "analysis_contract"
    ):
        return False
    if expected_capability_requirements is not None:
        required = set(expected_capability_requirements)
        if any(
            capability_id not in required
            for capability_id in capabilities[1:]
        ):
            return False
    if capabilities == ("analysis_contract",):
        return True
    return all(
        reviewed_queryless_gap_claim_types(
            capability_id,
            affected_claim_types,
            registry=registry,
        )
        for capability_id in capabilities[1:]
    )
