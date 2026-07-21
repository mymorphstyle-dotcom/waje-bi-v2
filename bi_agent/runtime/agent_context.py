from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence

from bi_agent.runtime.analysis_artifacts import (
    ArtifactDescriptor,
    PostgresAnalysisArtifactRegistry,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.thread_context_summary import (
    ThreadSummaryStore,
    VersionedThreadSummary,
)
from bi_agent.runtime.thread_item_ledger import ThreadHead, ThreadItem, ThreadItemLedger


class ArtifactIndex(Protocol):
    def list_artifacts(
        self,
        thread_id: str,
        *,
        limit: int,
    ) -> tuple[ArtifactDescriptor, ...]: ...


class ContextAuthorityReader(Protocol):
    def active_task(
        self,
        thread_id: str,
        active_task_id: str | None,
    ) -> Mapping[str, Any] | None: ...

    def accepted_decisions(
        self,
        active_task_id: str | None,
    ) -> tuple[Mapping[str, Any], ...]: ...


class EmptyContextAuthorityReader:
    def active_task(
        self,
        thread_id: str,
        active_task_id: str | None,
    ) -> Mapping[str, Any] | None:
        return None

    def accepted_decisions(
        self,
        active_task_id: str | None,
    ) -> tuple[Mapping[str, Any], ...]:
        return ()


class AgentContextError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AgentContextCompactionRequired(AgentContextError):
    def __init__(
        self,
        *,
        thread_id: str,
        compact_from_sequence: int,
        compact_through_sequence: int,
        latest_item_sequence: int,
        reason: str = "item_limit",
    ) -> None:
        super().__init__("agent_context_compaction_required")
        self.thread_id = thread_id
        self.compact_from_sequence = compact_from_sequence
        self.compact_through_sequence = compact_through_sequence
        self.latest_item_sequence = latest_item_sequence
        self.reason = reason


class AgentContextBudgetExceeded(AgentContextError):
    def __init__(self, *, estimated_tokens: int, budget_tokens: int) -> None:
        super().__init__("agent_context_token_budget_exceeded")
        self.estimated_tokens = estimated_tokens
        self.budget_tokens = budget_tokens


@dataclass(frozen=True)
class AgentContextSnapshot:
    thread_id: str
    thread_summary: Mapping[str, Any] | None
    recent_items: tuple[ThreadItem, ...]
    active_task: Mapping[str, Any] | None
    accepted_decisions: tuple[Mapping[str, Any], ...]
    pending_actions: tuple[Mapping[str, Any], ...]
    artifact_index: tuple[ArtifactDescriptor, ...]
    relevant_materials: tuple[Mapping[str, Any], ...]
    available_tools: tuple[Mapping[str, Any], ...]
    permission_scope: Mapping[str, Any]
    context_version: str
    thread_head: ThreadHead

    def to_dict(self, *, include_server_payload: bool = False) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "thread_summary": (
                deepcopy(dict(self.thread_summary))
                if self.thread_summary is not None
                else None
            ),
            "recent_items": [
                item.to_dict(include_server_payload=include_server_payload)
                for item in self.recent_items
            ],
            "active_task": (
                deepcopy(dict(self.active_task))
                if self.active_task is not None
                else None
            ),
            "accepted_decisions": [
                deepcopy(dict(item)) for item in self.accepted_decisions
            ],
            "pending_actions": [deepcopy(dict(item)) for item in self.pending_actions],
            "artifact_index": [item.to_dict() for item in self.artifact_index],
            "relevant_materials": [
                deepcopy(dict(item)) for item in self.relevant_materials
            ],
            "available_tools": [deepcopy(dict(item)) for item in self.available_tools],
            "permission_scope": deepcopy(dict(self.permission_scope)),
            "context_version": self.context_version,
            "thread_head": self.thread_head.to_dict(),
        }

    @property
    def material_refs(self) -> frozenset[str]:
        refs = {item.artifact_ref for item in self.artifact_index}
        for artifact in self.artifact_index:
            refs.update(artifact.source_refs)
        for material in self.relevant_materials:
            ref = material.get("material_ref") or material.get("ref")
            if isinstance(ref, str) and ref:
                refs.add(ref)
        return frozenset(refs)

    @property
    def compacted_through_sequence(self) -> int:
        if self.thread_summary is None:
            return 0
        value = self.thread_summary.get("coversThroughSequence")
        return int(value) if isinstance(value, int) else 0


class AgentContextAssembler:
    def __init__(
        self,
        *,
        ledger: ThreadItemLedger,
        artifact_index: ArtifactIndex,
        authority_reader: ContextAuthorityReader | None = None,
        summary_store: ThreadSummaryStore | None = None,
        recent_item_limit: int = 40,
        compaction_retention: int = 12,
        artifact_limit: int = 50,
        context_token_budget: int | None = None,
    ) -> None:
        if (
            recent_item_limit < 2
            or compaction_retention < 1
            or compaction_retention >= recent_item_limit
            or artifact_limit < 1
            or (
                context_token_budget is not None
                and (
                    isinstance(context_token_budget, bool)
                    or context_token_budget < 256
                )
            )
        ):
            raise ValueError("agent_context_limit_invalid")
        self._ledger = ledger
        self._artifact_index = artifact_index
        self._authority_reader = authority_reader or EmptyContextAuthorityReader()
        self._summary_store = summary_store
        self._recent_item_limit = recent_item_limit
        self._compaction_retention = compaction_retention
        self._artifact_limit = artifact_limit
        self._context_token_budget = context_token_budget

    def assemble(
        self,
        thread_id: str,
        *,
        available_tools: Sequence[Mapping[str, Any]] = (),
        permission_scope: Mapping[str, Any] | None = None,
        relevant_materials: Sequence[Mapping[str, Any]] = (),
    ) -> AgentContextSnapshot:
        head = self._ledger.get_head(thread_id)
        summary = self._latest_summary(thread_id, head)
        compacted_through = (
            summary.covers_through_sequence if summary is not None else 0
        )
        uncompacted_count = head.latest_item_sequence - compacted_through
        if uncompacted_count > self._recent_item_limit:
            raise AgentContextCompactionRequired(
                thread_id=thread_id,
                compact_from_sequence=compacted_through + 1,
                compact_through_sequence=(
                    head.latest_item_sequence - self._compaction_retention
                ),
                latest_item_sequence=head.latest_item_sequence,
                reason="item_limit",
            )
        recent_items = self._ledger.list_items(
            thread_id,
            limit=self._recent_item_limit,
            after_sequence=compacted_through,
        )
        artifacts = self._artifact_index.list_artifacts(
            thread_id,
            limit=self._artifact_limit,
        )
        active_task = self._authority_reader.active_task(
            thread_id,
            head.active_task_id,
        )
        decisions = self._authority_reader.accepted_decisions(head.active_task_id)
        pending_actions = (
            (
                {
                    "action_ref": head.pending_action_ref,
                    "authoritative": True,
                },
            )
            if head.pending_action_ref is not None
            else ()
        )
        normalized_tools = tuple(_mapping(item) for item in available_tools)
        normalized_materials = tuple(_mapping(item) for item in relevant_materials)
        normalized_permission = _mapping(permission_scope or {})
        version_payload = {
            "thread_id": thread_id,
            "head": head.to_dict(),
            "summary_digest": summary.summary_digest if summary is not None else None,
            "recent_item_digests": [item.item_digest for item in recent_items],
            "artifact_digests": [item.digest for item in artifacts],
            "decision_refs": [
                item.get("decision_id") or item.get("ref") for item in decisions
            ],
            "tool_digests": [
                canonical_digest(item) for item in normalized_tools
            ],
            "permission_scope": normalized_permission,
            "relevant_material_digests": [
                canonical_digest(item) for item in normalized_materials
            ],
        }
        snapshot = AgentContextSnapshot(
            thread_id=thread_id,
            thread_summary=(
                summary.to_contract() if summary is not None else None
            ),
            recent_items=recent_items,
            active_task=_optional_mapping(active_task),
            accepted_decisions=tuple(_mapping(item) for item in decisions),
            pending_actions=pending_actions,
            artifact_index=artifacts,
            relevant_materials=normalized_materials,
            available_tools=normalized_tools,
            permission_scope=normalized_permission,
            context_version=canonical_digest(version_payload),
            thread_head=head,
        )
        if self._context_token_budget is not None:
            estimated_tokens = self.estimated_input_tokens(snapshot)
            if estimated_tokens > self._context_token_budget:
                if uncompacted_count > self._compaction_retention:
                    raise AgentContextCompactionRequired(
                        thread_id=thread_id,
                        compact_from_sequence=compacted_through + 1,
                        compact_through_sequence=(
                            head.latest_item_sequence - self._compaction_retention
                        ),
                        latest_item_sequence=head.latest_item_sequence,
                        reason="token_budget",
                    )
                raise AgentContextBudgetExceeded(
                    estimated_tokens=estimated_tokens,
                    budget_tokens=self._context_token_budget,
                )
        return snapshot

    def _latest_summary(
        self,
        thread_id: str,
        head: ThreadHead,
    ) -> VersionedThreadSummary | None:
        if self._summary_store is None:
            return None
        summary = self._summary_store.latest(thread_id)
        if summary is None:
            return None
        if summary.thread_id != thread_id:
            raise AgentContextError("agent_context_summary_thread_mismatch")
        if summary.covers_through_sequence > head.latest_item_sequence:
            raise AgentContextError("agent_context_summary_ahead_of_thread")
        return summary

    @staticmethod
    def model_context(snapshot: AgentContextSnapshot) -> str:
        payload = snapshot.to_dict(include_server_payload=False)
        payload["recent_items"] = {
            "delivery": "postgres_agent_session",
            "count": len(snapshot.recent_items),
            "from_sequence": (
                snapshot.recent_items[0].sequence if snapshot.recent_items else None
            ),
            "through_sequence": (
                snapshot.recent_items[-1].sequence if snapshot.recent_items else None
            ),
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def estimated_input_tokens(cls, snapshot: AgentContextSnapshot) -> int:
        context_bytes = len(cls.model_context(snapshot).encode("utf-8"))
        replay_bytes = sum(
            len(item.text.encode("utf-8")) + 32 for item in snapshot.recent_items
        )
        return max(1, math.ceil((context_bytes + replay_bytes) / 2))


class InMemoryArtifactIndex:
    def __init__(self) -> None:
        self._by_thread: dict[str, list[ArtifactDescriptor]] = {}

    def add(self, thread_id: str, artifact: ArtifactDescriptor) -> None:
        self._by_thread.setdefault(thread_id, []).append(artifact)

    def list_artifacts(
        self,
        thread_id: str,
        *,
        limit: int,
    ) -> tuple[ArtifactDescriptor, ...]:
        return tuple(deepcopy(self._by_thread.get(thread_id, ())[-limit:]))


PostgresArtifactIndex = PostgresAnalysisArtifactRegistry


class PostgresContextAuthorityReader:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def active_task(
        self,
        thread_id: str,
        active_task_id: str | None,
    ) -> Mapping[str, Any] | None:
        if active_task_id is None:
            return None
        row = self.connection.execute(
            """
            SELECT run_id, topic_id, status, request, created_at, updated_at
            FROM waje_runtime.analysis_runs
            WHERE thread_id = %(thread_id)s AND run_id = %(run_id)s
            """,
            {"thread_id": thread_id, "run_id": active_task_id},
        ).fetchone()
        if row is None:
            return None
        request = _json_value(_field(row, "request", 3))
        return {
            "task_ref": str(_field(row, "run_id", 0)),
            "topic_ref": _field(row, "topic_id", 1),
            "status": str(_field(row, "status", 2)),
            "request": request if isinstance(request, Mapping) else {},
            "created_at": _isoformat(_field(row, "created_at", 4)),
            "updated_at": _isoformat(_field(row, "updated_at", 5)),
        }

    def accepted_decisions(
        self,
        active_task_id: str | None,
    ) -> tuple[Mapping[str, Any], ...]:
        if active_task_id is None:
            return ()
        rows = self.connection.execute(
            """
            SELECT decision.decision_id, decision.slot_id, decision.option_id,
                   decision.source, decision.status, decision.materiality,
                   decision.content_digest, decision.payload
            FROM waje_runtime.decision_records decision
            JOIN waje_runtime.intent_revisions revision
              ON revision.intent_revision_id = decision.intent_revision_id
            LEFT JOIN waje_runtime.intent_revision_supersessions supersession
              ON supersession.superseded_intent_revision_id = revision.intent_revision_id
            WHERE decision.run_attempt_id = %(run_id)s
              AND decision.status <> 'invalidated'
              AND supersession.supersession_id IS NULL
            ORDER BY decision.ledger_position
            """,
            {"run_id": active_task_id},
        ).fetchall()
        return tuple(
            {
                "decision_id": str(_field(row, "decision_id", 0)),
                "slot_id": str(_field(row, "slot_id", 1)),
                "option_id": _field(row, "option_id", 2),
                "source": str(_field(row, "source", 3)),
                "status": str(_field(row, "status", 4)),
                "materiality": str(_field(row, "materiality", 5)),
                "digest": str(_field(row, "content_digest", 6)),
                "payload": _json_value(_field(row, "payload", 7)),
            }
            for row in rows
        )


def _mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = canonical_value(value)
    if not isinstance(normalized, dict):
        raise ValueError("agent_context_mapping_invalid")
    return normalized


def _optional_mapping(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return _mapping(value) if value is not None else None


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _field(row: Any, key: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index]


def _isoformat(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
