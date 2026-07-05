from collections.abc import Iterable, Mapping
from typing import Optional

from bi_agent.runtime.models import (
    CompiledGraph,
    GraphNode,
    MutationLedger,
    MutationRecord,
    RecipeEntry,
)
from bi_agent.runtime.recipe_registry import load_recipe_registry


REQUIRED_PATTERN_PATHS = (
    "data_quality_check",
    "pattern_scan",
    "formula_decompose",
    "event_evidence",
    "segment_bridge",
    "outlier_scan",
    "answer_verify",
)

SUPPORTED_CAPABILITIES = frozenset((*REQUIRED_PATTERN_PATHS, "joint_attribution"))


def compile_graph(
    *,
    question_family: str,
    target_metric: str,
    pattern_family: Optional[str] = None,
    requested_nodes: Iterable[str] = (),
    registry: Optional[Mapping[str, RecipeEntry]] = None,
) -> CompiledGraph:
    registry = registry or load_recipe_registry()
    proposed_graph = tuple(requested_nodes)
    recipe = registry.get(question_family)

    if recipe is None:
        return _compiled(
            status="rejected",
            target_metric=target_metric,
            accepted=(),
            proposed=proposed_graph,
            rejected_or_degraded=(question_family,),
            records=(
                MutationRecord(
                    action="rejected",
                    capability=question_family,
                    reason="unknown_question_family",
                ),
            ),
        )

    if not proposed_graph:
        proposed_graph = recipe.subgraph_nodes

    unknown = tuple(node for node in proposed_graph if node not in SUPPORTED_CAPABILITIES)
    known_requested = tuple(node for node in proposed_graph if node in SUPPORTED_CAPABILITIES)
    records = tuple(
        MutationRecord(
            action="rejected",
            capability=node,
            reason="unknown_capability",
        )
        for node in unknown
    )

    if question_family == "pattern_explanation":
        accepted = _dedupe((*known_requested, *REQUIRED_PATTERN_PATHS))
        auto_added = tuple(node for node in accepted if node not in known_requested)
        records = (
            *records,
            *(
                MutationRecord(
                    action="auto_added",
                    capability=node,
                    reason=f"required_pattern_path:{pattern_family or 'unspecified'}",
                )
                for node in auto_added
            ),
        )
        return _compiled(
            status="accepted" if not unknown else "degraded",
            target_metric=target_metric,
            accepted=accepted,
            proposed=proposed_graph,
            rejected_or_degraded=unknown,
            records=records,
        )

    accepted = tuple(node for node in recipe.subgraph_nodes if node in SUPPORTED_CAPABILITIES)
    records = (
        *records,
        *(
            MutationRecord(
                action="degraded",
                capability=node,
                reason="non_pattern_dry_run_skeleton",
            )
            for node in accepted
        ),
    )
    return _compiled(
        status="degraded",
        target_metric=target_metric,
        accepted=accepted,
        proposed=proposed_graph,
        rejected_or_degraded=(*unknown, *accepted),
        records=records,
    )


def _compiled(
    *,
    status: str,
    target_metric: str,
    accepted: tuple[str, ...],
    proposed: tuple[str, ...],
    rejected_or_degraded: tuple[str, ...],
    records: tuple[MutationRecord, ...],
) -> CompiledGraph:
    return CompiledGraph(
        status=status,
        accepted_nodes=tuple(
            GraphNode(
                node_id=f"{index:02d}_{capability}",
                capability=capability,
                status="accepted" if status == "accepted" else "degraded",
                target_claim=target_metric,
                depends_on=accepted[:index],
            )
            for index, capability in enumerate(accepted)
        ),
        mutations=MutationLedger(
            proposed_graph=proposed,
            accepted_graph=accepted,
            rejected_or_degraded=rejected_or_degraded,
            records=records,
        ),
    )


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
