from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RecipeEntry:
    recipe_id: str
    question_family: str
    subgraph_nodes: tuple[str, ...]
    default_degraded: bool = False


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    capability: str
    status: str
    target_claim: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class MutationRecord:
    action: str
    capability: str
    reason: str


@dataclass(frozen=True)
class MutationLedger:
    proposed_graph: tuple[str, ...]
    accepted_graph: tuple[str, ...]
    rejected_or_degraded: tuple[str, ...]
    records: tuple[MutationRecord, ...]


@dataclass(frozen=True)
class CompiledGraph:
    status: str
    accepted_nodes: tuple[GraphNode, ...]
    mutations: MutationLedger
    runtime_plan: dict[str, Any] = field(default_factory=dict)
