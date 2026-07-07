from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from bi_agent.conversation.models import (
    ArtifactRef,
    MemoryItem,
    MemoryProposal,
    ResultRefRecord,
    ThreadState,
    TopicState,
)


ROOT = Path(__file__).resolve().parents[2]
CONVERSATION_SCHEMA_SQL = (ROOT / "tools" / "runtime" / "conversation-runtime.sql").read_text(
    encoding="utf-8"
)


class PostgresConversationStore:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    @classmethod
    def from_env(cls) -> "PostgresConversationStore":
        dsn = os.environ.get("WAJE_RUNTIME_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("WAJE_RUNTIME_DATABASE_URL or DATABASE_URL is required")
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("psycopg is required for PostgresConversationStore") from exc
        return cls(psycopg.connect(dsn))

    def apply_schema(self) -> None:
        self.connection.execute(CONVERSATION_SCHEMA_SQL)
        self.connection.commit()

    def create_thread(self, thread_id: Optional[str] = None, *, owner_id: str = "user") -> ThreadState:
        thread_id = thread_id or f"thread-{uuid4().hex[:12]}"
        self._execute(
            """
            INSERT INTO waje_runtime.investigation_threads(thread_id, owner_id)
            VALUES (%(thread_id)s, %(owner_id)s)
            ON CONFLICT (thread_id) DO UPDATE
            SET owner_id = EXCLUDED.owner_id, updated_at = now()
            """,
            {"thread_id": thread_id, "owner_id": owner_id},
        )
        self._audit("thread_created", thread_id=thread_id, ref=thread_id, payload={"owner_id": owner_id})
        return ThreadState(thread_id=thread_id, owner_id=owner_id)

    def get_thread(self, thread_id: str) -> ThreadState:
        row = self._fetchone(
            """
            SELECT thread_id, owner_id, current_topic_id, pending_clarification_topic_id, pending_clarification_id
            FROM waje_runtime.investigation_threads
            WHERE thread_id = %(thread_id)s
            """,
            {"thread_id": thread_id},
        )
        if not row:
            return self.create_thread(thread_id)
        return ThreadState(
            thread_id=_field(row, "thread_id", 0),
            owner_id=_field(row, "owner_id", 1),
            current_topic_id=_field(row, "current_topic_id", 2),
            pending_clarification_topic_id=_field(row, "pending_clarification_topic_id", 3),
            pending_clarification_id=_field(row, "pending_clarification_id", 4) or "",
        )

    def create_topic(self, thread_id: str, *, title: str, summary: str = "") -> TopicState:
        self.get_thread(thread_id)
        topic_id = f"topic-{uuid4().hex[:12]}"
        self._execute(
            """
            INSERT INTO waje_runtime.conversation_topics(topic_id, thread_id, title, summary)
            VALUES (%(topic_id)s, %(thread_id)s, %(title)s, %(summary)s)
            """,
            {
                "topic_id": topic_id,
                "thread_id": thread_id,
                "title": title,
                "summary": summary or title,
            },
        )
        self._audit("topic_created", thread_id=thread_id, topic_id=topic_id, ref=topic_id)
        return TopicState(topic_id=topic_id, thread_id=thread_id, title=title, summary=summary or title)

    def set_current_topic(self, thread_id: str, topic_id: str) -> None:
        self._execute(
            """
            UPDATE waje_runtime.investigation_threads
            SET current_topic_id = %(topic_id)s, updated_at = now()
            WHERE thread_id = %(thread_id)s
            """,
            {"thread_id": thread_id, "topic_id": topic_id},
        )
        self._audit("current_topic_set", thread_id=thread_id, topic_id=topic_id, ref=topic_id)

    def current_topic(self, thread_id: str) -> Optional[TopicState]:
        thread = self.get_thread(thread_id)
        return self.topic(thread.current_topic_id)

    def topic(self, topic_id: Optional[str]) -> Optional[TopicState]:
        if not topic_id:
            return None
        row = self._fetchone(
            """
            SELECT topic_id, thread_id, title, summary, status
            FROM waje_runtime.conversation_topics
            WHERE topic_id = %(topic_id)s
            """,
            {"topic_id": topic_id},
        )
        if not row:
            return None
        return TopicState(
            topic_id=_field(row, "topic_id", 0),
            thread_id=_field(row, "thread_id", 1),
            title=_field(row, "title", 2),
            summary=_field(row, "summary", 3),
            status=_field(row, "status", 4) or "active",
        )

    def topics_for_thread(self, thread_id: str) -> tuple[TopicState, ...]:
        rows = self._fetchall(
            """
            SELECT topic_id, thread_id, title, summary, status
            FROM waje_runtime.conversation_topics
            WHERE thread_id = %(thread_id)s
            ORDER BY created_at
            """,
            {"thread_id": thread_id},
        )
        return tuple(
            TopicState(
                topic_id=_field(row, "topic_id", 0),
                thread_id=_field(row, "thread_id", 1),
                title=_field(row, "title", 2),
                summary=_field(row, "summary", 3),
                status=_field(row, "status", 4) or "active",
            )
            for row in rows
        )

    def set_pending_clarification(self, thread_id: str, topic_id: str, clarification_id: str) -> None:
        self._execute(
            """
            UPDATE waje_runtime.investigation_threads
            SET pending_clarification_topic_id = %(topic_id)s,
                pending_clarification_id = %(clarification_id)s,
                updated_at = now()
            WHERE thread_id = %(thread_id)s
            """,
            {"thread_id": thread_id, "topic_id": topic_id, "clarification_id": clarification_id},
        )
        self._audit("clarification_pending", thread_id=thread_id, topic_id=topic_id, ref=clarification_id)

    def clear_pending_clarification(self, thread_id: str) -> None:
        self._execute(
            """
            UPDATE waje_runtime.investigation_threads
            SET pending_clarification_topic_id = NULL,
                pending_clarification_id = '',
                updated_at = now()
            WHERE thread_id = %(thread_id)s
            """,
            {"thread_id": thread_id},
        )
        self._audit("clarification_cleared", thread_id=thread_id)

    def add_turn(self, thread_id: str, turn: dict[str, Any]) -> None:
        turn_id = str(turn.get("turn_id") or turn.get("turnId") or f"turn-{uuid4().hex[:12]}")
        topic_id = turn.get("topic_id") or turn.get("topicId")
        intent = str(turn.get("intent") or turn.get("turn_intent", {}).get("intent", ""))
        self._execute(
            """
            INSERT INTO waje_runtime.conversation_turns(turn_id, thread_id, topic_id, intent, payload)
            VALUES (%(turn_id)s, %(thread_id)s, %(topic_id)s, %(intent)s, %(payload)s::jsonb)
            ON CONFLICT (turn_id) DO UPDATE
            SET payload = EXCLUDED.payload
            """,
            {
                "turn_id": turn_id,
                "thread_id": thread_id,
                "topic_id": topic_id,
                "intent": intent,
                "payload": _json(turn),
            },
        )
        self._audit("turn_recorded", thread_id=thread_id, topic_id=topic_id, ref=turn_id, payload={"intent": intent})

    def add_result_ref(
        self,
        topic_id: str,
        *,
        result_ref: str,
        snapshot_id: str,
        contract_version: str,
        permission_scope: str,
        semantic_scope: str,
    ) -> None:
        self._execute(
            """
            INSERT INTO waje_runtime.result_refs(
              result_ref, topic_id, snapshot_id, contract_version, permission_scope, semantic_scope
            )
            VALUES (
              %(result_ref)s, %(topic_id)s, %(snapshot_id)s, %(contract_version)s,
              %(permission_scope)s, %(semantic_scope)s
            )
            ON CONFLICT (result_ref) DO UPDATE
            SET snapshot_id = EXCLUDED.snapshot_id,
                contract_version = EXCLUDED.contract_version,
                permission_scope = EXCLUDED.permission_scope,
                semantic_scope = EXCLUDED.semantic_scope
            """,
            {
                "result_ref": result_ref,
                "topic_id": topic_id,
                "snapshot_id": snapshot_id,
                "contract_version": contract_version,
                "permission_scope": permission_scope,
                "semantic_scope": semantic_scope,
            },
        )
        self._audit("result_ref_recorded", topic_id=topic_id, ref=result_ref)

    def results_for_topic(self, topic_id: Optional[str]) -> tuple[ResultRefRecord, ...]:
        if not topic_id:
            return ()
        rows = self._fetchall(
            """
            SELECT topic_id, result_ref, snapshot_id, contract_version, permission_scope, semantic_scope
            FROM waje_runtime.result_refs
            WHERE topic_id = %(topic_id)s
            ORDER BY created_at DESC
            """,
            {"topic_id": topic_id},
        )
        return tuple(
            ResultRefRecord(
                topic_id=_field(row, "topic_id", 0),
                result_ref=_field(row, "result_ref", 1),
                snapshot_id=_field(row, "snapshot_id", 2),
                contract_version=_field(row, "contract_version", 3),
                permission_scope=_field(row, "permission_scope", 4),
                semantic_scope=_field(row, "semantic_scope", 5),
            )
            for row in rows
        )

    def add_artifact(
        self,
        *,
        artifact_id: str,
        topic_id: str,
        follow_up_context: str,
        snapshot_id: str,
        permission_scope: str,
    ) -> None:
        self._audit(
            "artifact_linked",
            topic_id=topic_id,
            ref=artifact_id,
            payload={
                "follow_up_context": follow_up_context,
                "snapshot_id": snapshot_id,
                "permission_scope": permission_scope,
            },
        )

    def latest_artifact_for_topic(self, topic_id: Optional[str]) -> Optional[ArtifactRef]:
        return None

    def add_memory_item(
        self,
        *,
        owner_scope: str,
        text: str,
        source_ref: str,
        visibility: str,
        status: str,
    ) -> MemoryItem:
        item = MemoryItem(
            memory_id=f"memory-{uuid4().hex[:12]}",
            owner_scope=owner_scope,
            text=text,
            source_ref=source_ref,
            visibility=visibility,
            status=status,
        )
        self._execute(
            """
            INSERT INTO waje_runtime.memory_items(
              memory_id, owner_scope, text, source_ref, visibility, status, ttl, confidence
            )
            VALUES (
              %(memory_id)s, %(owner_scope)s, %(text)s, %(source_ref)s,
              %(visibility)s, %(status)s, %(ttl)s, %(confidence)s
            )
            """,
            item.__dict__,
        )
        self._audit("memory_item_recorded", ref=item.memory_id, payload={"owner_scope": owner_scope})
        return item

    def long_term_memory(self, owner_scope: str) -> tuple[MemoryItem, ...]:
        rows = self._fetchall(
            """
            SELECT memory_id, owner_scope, text, source_ref, visibility, status, ttl, confidence
            FROM waje_runtime.memory_items
            WHERE owner_scope = %(owner_scope)s AND status = 'accepted' AND revoked_at IS NULL
            ORDER BY created_at DESC
            """,
            {"owner_scope": owner_scope},
        )
        return tuple(
            MemoryItem(
                memory_id=_field(row, "memory_id", 0),
                owner_scope=_field(row, "owner_scope", 1),
                text=_field(row, "text", 2),
                source_ref=_field(row, "source_ref", 3),
                visibility=_field(row, "visibility", 4),
                status=_field(row, "status", 5),
                ttl=_field(row, "ttl", 6),
                confidence=_field(row, "confidence", 7),
            )
            for row in rows
        )

    def add_memory_proposal(self, proposal: MemoryProposal) -> None:
        self._execute(
            """
            INSERT INTO waje_runtime.memory_proposals(
              proposal_id, thread_id, text, source_ref, owner_scope, visibility, status
            )
            VALUES (
              %(proposal_id)s, %(thread_id)s, %(text)s, %(source_ref)s,
              %(owner_scope)s, %(visibility)s, %(status)s
            )
            ON CONFLICT (proposal_id) DO UPDATE
            SET status = EXCLUDED.status
            """,
            proposal.__dict__,
        )
        self._audit(
            "memory_proposal_recorded",
            thread_id=proposal.thread_id,
            ref=proposal.proposal_id,
            payload={"owner_scope": proposal.owner_scope},
        )

    def accept_memory_proposal(self, proposal_id: str) -> Optional[MemoryItem]:
        self._execute(
            """
            UPDATE waje_runtime.memory_proposals
            SET status = 'accepted', decided_at = now()
            WHERE proposal_id = %(proposal_id)s
            """,
            {"proposal_id": proposal_id},
        )
        self._audit("memory_proposal_accepted", ref=proposal_id)
        return None

    def _execute(self, statement: str, params: Optional[dict[str, Any]] = None) -> Any:
        result = self.connection.execute(statement, params or {})
        self.connection.commit()
        return result

    def _fetchone(self, statement: str, params: Optional[dict[str, Any]] = None) -> Any:
        return self.connection.execute(statement, params or {}).fetchone()

    def _fetchall(self, statement: str, params: Optional[dict[str, Any]] = None) -> list[Any]:
        return list(self.connection.execute(statement, params or {}).fetchall())

    def _audit(
        self,
        event_type: str,
        *,
        actor_id: str = "",
        thread_id: Optional[str] = None,
        topic_id: Optional[str] = None,
        run_id: Optional[str] = None,
        ref: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        self._execute(
            """
            INSERT INTO waje_runtime.audit_events(
              event_type, actor_id, thread_id, topic_id, run_id, ref, payload
            )
            VALUES (
              %(event_type)s, %(actor_id)s, %(thread_id)s, %(topic_id)s,
              %(run_id)s, %(ref)s, %(payload)s::jsonb
            )
            """,
            {
                "event_type": event_type,
                "actor_id": actor_id,
                "thread_id": thread_id,
                "topic_id": topic_id,
                "run_id": run_id,
                "ref": ref,
                "payload": _json(payload or {}),
            },
        )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _field(row: Any, key: str, index: int) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[index]
    except (IndexError, TypeError):
        return None
