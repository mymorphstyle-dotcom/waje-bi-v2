from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from uuid import uuid4

from bi_agent.conversation.models import (
    ArtifactRef,
    ClarificationOption,
    ClarificationState,
    ContextItem,
    ContextManifest,
    MemoryItem,
    MemoryProposal,
    ReuseDecision,
    ResultRefRecord,
    ThreadState,
    TopicState,
    validate_result_reuse_candidate,
)
from bi_agent.runtime.analysis_assets import asset_dedup_key, merge_analysis_assets
from bi_agent.runtime.dataset_catalog import (
    DatasetReleaseAuthorityRecord,
    build_dataset_release_authority_record,
    canonical_dataset_release_members,
    canonical_dataset_requires_release,
    dataset_release_authority_record_from_mapping,
    immutable_dataset_snapshot_projection,
    validate_dataset_snapshot_release_payloads,
)
from bi_agent.runtime.runtime_publication_index import (
    runtime_publication_index as _runtime_publication_index,
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

    def recover_after_write_failure(self) -> None:
        self.connection.rollback()

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

    def save_clarification_state(self, state: ClarificationState) -> None:
        row = self._fetchone(
            """
            SELECT COALESCE(r.thread_id, t.thread_id) AS thread_id
            FROM (SELECT %(run_id)s::text AS run_id, %(topic_id)s::text AS topic_id) state
            LEFT JOIN waje_runtime.analysis_runs r ON r.run_id = state.run_id
            LEFT JOIN waje_runtime.conversation_topics t ON t.topic_id = state.topic_id
            LIMIT 1
            """,
            {"run_id": state.run_id, "topic_id": state.topic_id},
        )
        thread_id = _field(row, "thread_id", 0) if row else None
        if state.status == "waiting" and thread_id:
            self._execute(
                """
                UPDATE waje_runtime.investigation_threads
                SET pending_clarification_topic_id = %(topic_id)s,
                    pending_clarification_id = COALESCE(NULLIF(pending_clarification_id, ''), %(run_id)s),
                    updated_at = now()
                WHERE thread_id = %(thread_id)s
                """,
                {"thread_id": thread_id, "topic_id": state.topic_id, "run_id": state.run_id},
            )
        self._audit(
            "clarification_state_saved",
            thread_id=thread_id,
            topic_id=state.topic_id,
            run_id=state.run_id,
            ref=state.run_id,
            payload=state.to_dict(),
        )

    def get_open_clarification(self, thread_id: str) -> Optional[ClarificationState]:
        rows = self._fetchall(
            """
            SELECT payload
            FROM waje_runtime.audit_events
            WHERE thread_id = %(thread_id)s
              AND event_type = 'clarification_state_saved'
            ORDER BY created_at DESC, audit_id DESC
            LIMIT 50
            """,
            {"thread_id": thread_id},
        )
        seen: set[str] = set()
        for row in rows:
            state = _clarification_state_from_payload(_field(row, "payload", 0))
            if not state or state.run_id in seen:
                continue
            seen.add(state.run_id)
            if state.status == "waiting":
                return state
        thread = self.get_thread(thread_id)
        if thread.pending_clarification_id and thread.pending_clarification_topic_id:
            return ClarificationState(
                run_id=thread.pending_clarification_id,
                topic_id=thread.pending_clarification_topic_id,
                question="待确认的业务澄清问题",
                options=[],
            )
        return None

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

    def upsert_run(
        self,
        run_id: str,
        *,
        thread_id: str,
        turn_id: str = "",
        topic_id: str = "",
        status: str,
        request: Optional[dict[str, Any]] = None,
    ) -> None:
        self._execute(
            """
            INSERT INTO waje_runtime.analysis_runs(run_id, thread_id, turn_id, topic_id, status, request)
            VALUES (
              %(run_id)s, %(thread_id)s, %(turn_id)s, %(topic_id)s,
              %(status)s, %(request)s::jsonb
            )
            ON CONFLICT (run_id) DO UPDATE
            SET status = EXCLUDED.status,
                request = EXCLUDED.request,
                turn_id = EXCLUDED.turn_id,
                topic_id = EXCLUDED.topic_id,
                updated_at = now()
            """,
            {
                "run_id": run_id,
                "thread_id": thread_id,
                "turn_id": turn_id or None,
                "topic_id": topic_id or None,
                "status": status,
                "request": _json(request or {}),
            },
        )
        self._audit("run_status_changed", thread_id=thread_id, topic_id=topic_id, run_id=run_id, ref=run_id)

    def get_run_request(self, run_id: str) -> dict[str, Any]:
        row = self._fetchone(
            "SELECT request, thread_id, topic_id FROM waje_runtime.analysis_runs WHERE run_id = %(run_id)s",
            {"run_id": run_id},
        )
        value = _field(row, "request", 0) if row else {}
        if isinstance(value, str):
            value = json.loads(value)
        request = dict(value) if isinstance(value, Mapping) else {}
        request["thread_id"] = str(_field(row, "thread_id", 1) or "") if row else ""
        request["topic_id"] = str(_field(row, "topic_id", 2) or "") if row else ""
        return request

    def record_clarification_outcome(
        self,
        *,
        source_run_id: str,
        thread_id: str,
        topic_id: str,
        choice: Mapping[str, Any],
    ) -> str:
        from bi_agent.conversation.clarification_authority import (
            build_clarification_outcome,
        )
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        owner = self._fetchone(
            """
            SELECT thread_id, topic_id, status
            FROM waje_runtime.analysis_runs
            WHERE run_id = %(source_run_id)s
            FOR UPDATE
            """,
            {"source_run_id": source_run_id},
        )
        if owner is None:
            self.connection.rollback()
            raise EvidenceIntegrityError("clarification_outcome_source_run_missing")
        if str(_field(owner, "status", 2) or "") != "waiting_for_clarification":
            self.connection.rollback()
            raise EvidenceIntegrityError("clarification_outcome_source_run_stale")
        if (
            str(_field(owner, "thread_id", 0) or "") != thread_id
            or str(_field(owner, "topic_id", 1) or "") != topic_id
        ):
            self.connection.rollback()
            raise EvidenceIntegrityError("clarification_outcome_owner_mismatch")

        payload = build_clarification_outcome(
            source_run_id=source_run_id,
            thread_id=thread_id,
            topic_id=topic_id,
            choice=choice,
        )
        self._audit(
            "clarification_outcome_recorded",
            thread_id=thread_id,
            topic_id=topic_id,
            run_id=source_run_id,
            ref=payload["outcome_ref"],
            payload=payload,
        )
        return str(payload["outcome_ref"])

    def resolve_clarification_resume_authority(
        self,
        *,
        source_run_id: str,
        thread_id: str,
        topic_id: str,
        choice: Mapping[str, Any],
        outcome_ref: str,
    ) -> dict[str, Any]:
        from bi_agent.conversation.clarification_authority import (
            validate_clarification_resume_authority,
        )
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        row = self._fetchone(
            """
            SELECT ac.analysis_contract_id,
                   ac.run_id AS analysis_run_id,
                   ac.contract_signature AS stored_contract_signature,
                   ac.payload AS contract_payload,
                   r.status AS run_status,
                   r.thread_id AS run_thread_id,
                   r.topic_id AS run_topic_id,
                   r.request AS run_request,
                   e.payload AS outcome_payload,
                   e.ref AS outcome_ref,
                   e.run_id AS outcome_run_id,
                   e.thread_id AS outcome_thread_id,
                   e.topic_id AS outcome_topic_id
            FROM waje_runtime.analysis_runs r
            JOIN waje_runtime.analysis_contracts ac
              ON ac.run_id = r.run_id
             AND ac.analysis_contract_id = %(analysis_contract_id)s
            JOIN waje_runtime.audit_events e
              ON e.event_type = 'clarification_outcome_recorded'
             AND e.ref = %(outcome_ref)s
             AND e.run_id = r.run_id
            WHERE r.run_id = %(source_run_id)s
            """,
            {
                "source_run_id": source_run_id,
                "analysis_contract_id": f"analysis:{source_run_id}:1",
                "outcome_ref": outcome_ref,
            },
        )
        if row is None:
            raise EvidenceIntegrityError("clarification_resume_authority_missing")
        run_request = _json_value(_field(row, "run_request", 7)) or {}
        return validate_clarification_resume_authority(
            source_run_id=source_run_id,
            thread_id=thread_id,
            topic_id=topic_id,
            choice=choice,
            outcome_ref=outcome_ref,
            analysis_contract=_json_value(_field(row, "contract_payload", 3)) or {},
            stored_contract_signature=str(_field(row, "stored_contract_signature", 2) or ""),
            analysis_run_id=str(_field(row, "analysis_run_id", 1) or ""),
            run_status=str(_field(row, "run_status", 4) or ""),
            run_thread_id=str(_field(row, "run_thread_id", 5) or ""),
            run_topic_id=str(_field(row, "run_topic_id", 6) or ""),
            clarification_outcome=_json_value(_field(row, "outcome_payload", 8)) or {},
            outcome_run_id=str(_field(row, "outcome_run_id", 10) or ""),
            outcome_thread_id=str(_field(row, "outcome_thread_id", 11) or ""),
            outcome_topic_id=str(_field(row, "outcome_topic_id", 12) or ""),
            material_authority=(
                run_request.get("material_authority")
                if isinstance(run_request, Mapping)
                else None
            ),
        )

    def resolve_completed_material_authority(
        self,
        *,
        source_run_id: str,
        thread_id: str,
        topic_id: str,
    ) -> dict[str, Any]:
        from bi_agent.conversation.clarification_authority import (
            validate_completed_followup_authority,
        )
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        rows = self._fetchall(
            """
            /* completed_material_authority */
            SELECT ac.analysis_contract_id,
                   ac.run_id AS analysis_run_id,
                   ac.contract_signature AS stored_contract_signature,
                   ac.payload AS contract_payload,
                   r.status AS run_status,
                   r.thread_id AS run_thread_id,
                   r.topic_id AS run_topic_id,
                   r.request AS run_request,
                   e.payload AS authority_record_payload,
                   e.ref AS authority_record_ref,
                   e.run_id AS authority_event_run_id,
                   e.thread_id AS authority_event_thread_id,
                   e.topic_id AS authority_event_topic_id
            FROM waje_runtime.analysis_runs r
            LEFT JOIN waje_runtime.analysis_contracts ac
              ON ac.run_id = r.run_id
             AND ac.analysis_contract_id = %(analysis_contract_id)s
            LEFT JOIN waje_runtime.audit_events e
              ON e.run_id = r.run_id
             AND e.event_type = 'completed_material_authority_recorded'
            WHERE r.run_id = %(source_run_id)s
            """,
            {
                "source_run_id": source_run_id,
                "analysis_contract_id": f"analysis:{source_run_id}:1",
            },
        )
        if not rows:
            raise EvidenceIntegrityError(
                "completed_followup_source_run_missing"
            )
        if not str(_field(rows[0], "analysis_run_id", 1) or ""):
            raise EvidenceIntegrityError("completed_followup_contract_missing")
        event_rows = [
            row
            for row in rows
            if str(_field(row, "authority_event_run_id", 10) or "")
        ]
        if not event_rows:
            raise EvidenceIntegrityError(
                "completed_followup_authority_record_missing"
            )
        if len(event_rows) != 1:
            raise EvidenceIntegrityError(
                "completed_followup_authority_record_ambiguous"
            )
        row = event_rows[0]
        request = _json_value(_field(row, "run_request", 7)) or {}
        return validate_completed_followup_authority(
            source_run_id=source_run_id,
            thread_id=thread_id,
            topic_id=topic_id,
            analysis_contract=_json_value(_field(row, "contract_payload", 3)) or {},
            stored_contract_signature=str(
                _field(row, "stored_contract_signature", 2) or ""
            ),
            analysis_run_id=str(_field(row, "analysis_run_id", 1) or ""),
            run_status=str(_field(row, "run_status", 4) or ""),
            run_thread_id=str(_field(row, "run_thread_id", 5) or ""),
            run_topic_id=str(_field(row, "run_topic_id", 6) or ""),
            request_analysis_contract=(
                request.get("analysis_contract")
                if isinstance(request, Mapping)
                else None
            ),
            material_authority=(
                request.get("material_authority")
                if isinstance(request, Mapping)
                else None
            ),
            authority_record=(
                _json_value(_field(row, "authority_record_payload", 8)) or {}
            ),
            authority_event_ref=str(
                _field(row, "authority_record_ref", 9) or ""
            ),
            authority_event_run_id=str(
                _field(row, "authority_event_run_id", 10) or ""
            ),
            authority_event_thread_id=str(
                _field(row, "authority_event_thread_id", 11) or ""
            ),
            authority_event_topic_id=str(
                _field(row, "authority_event_topic_id", 12) or ""
            ),
        )

    def finalize_completed_material_authority(
        self,
        *,
        run_id: str,
        thread_id: str,
        topic_id: str,
        request: Mapping[str, Any],
        material_authority: Mapping[str, Any],
    ) -> dict[str, Any]:
        from bi_agent.conversation.clarification_authority import (
            build_completed_material_authority_record,
            validate_completed_followup_authority,
        )
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        try:
            run_row = self._fetchone(
                """
                /* completed_material_authority_finalization_run_lock */
                SELECT r.status AS run_status,
                       r.thread_id AS run_thread_id,
                       r.topic_id AS run_topic_id,
                       r.request AS run_request
                FROM waje_runtime.analysis_runs r
                WHERE r.run_id = %(run_id)s
                FOR UPDATE
                """,
                {"run_id": run_id},
            )
            if run_row is None:
                raise EvidenceIntegrityError(
                    "completed_followup_source_run_missing"
                )
            contract_row = self._fetchone(
                """
                /* completed_material_authority_finalization_contract_lock */
                SELECT ac.run_id AS analysis_run_id,
                       ac.contract_signature AS stored_contract_signature,
                       ac.payload AS contract_payload
                FROM waje_runtime.analysis_contracts ac
                WHERE ac.run_id = %(run_id)s
                  AND ac.analysis_contract_id = %(analysis_contract_id)s
                FOR UPDATE
                """,
                {
                    "run_id": run_id,
                    "analysis_contract_id": f"analysis:{run_id}:1",
                },
            )
            if (
                contract_row is None
                or not str(
                    _field(contract_row, "analysis_run_id", 0) or ""
                )
            ):
                raise EvidenceIntegrityError(
                    "completed_followup_contract_missing"
                )
            run_status = str(_field(run_row, "run_status", 0) or "")
            run_thread_id = str(
                _field(run_row, "run_thread_id", 1) or ""
            )
            run_topic_id = str(_field(run_row, "run_topic_id", 2) or "")
            if (run_thread_id, run_topic_id) != (thread_id, topic_id):
                raise EvidenceIntegrityError(
                    "completed_followup_owner_mismatch"
                )
            stored_signature = str(
                _field(
                    contract_row,
                    "stored_contract_signature",
                    1,
                )
                or ""
            )
            contract = (
                _json_value(_field(contract_row, "contract_payload", 2))
                or {}
            )
            if not isinstance(contract, Mapping):
                raise EvidenceIntegrityError(
                    "completed_followup_contract_payload_invalid"
                )
            embedded_signature = str(
                contract.get("contract_signature") or ""
            )
            if (
                not embedded_signature
                or embedded_signature != stored_signature
            ):
                raise EvidenceIntegrityError(
                    "completed_followup_contract_signature_invalid"
                )
            authoritative_contract = canonical_value(
                {
                    **dict(contract),
                    "contract_signature": embedded_signature,
                }
            )
            finalized_request = canonical_value(
                {
                    **dict(request),
                    "analysis_contract": authoritative_contract,
                    "material_authority": material_authority,
                }
            )
            record = build_completed_material_authority_record(
                source_run_id=run_id,
                thread_id=thread_id,
                topic_id=topic_id,
                analysis_contract=authoritative_contract,
                material_authority=material_authority,
            )
            validate_completed_followup_authority(
                source_run_id=run_id,
                thread_id=thread_id,
                topic_id=topic_id,
                analysis_contract=authoritative_contract,
                stored_contract_signature=stored_signature,
                analysis_run_id=str(
                    _field(contract_row, "analysis_run_id", 0) or ""
                ),
                run_status="completed",
                run_thread_id=thread_id,
                run_topic_id=topic_id,
                request_analysis_contract=authoritative_contract,
                material_authority=material_authority,
                authority_record=record,
                authority_event_ref=f"completed-material-authority:{run_id}",
                authority_event_run_id=run_id,
                authority_event_thread_id=thread_id,
                authority_event_topic_id=topic_id,
            )
            existing = self._fetchall(
                """
                /* completed_material_authority_existing_events */
                SELECT payload, ref, run_id, thread_id, topic_id
                FROM waje_runtime.audit_events
                WHERE event_type = 'completed_material_authority_recorded'
                  AND run_id = %(run_id)s
                FOR UPDATE
                """,
                {"run_id": run_id},
            )
            if run_status == "completed":
                stored_request = (
                    _json_value(_field(run_row, "run_request", 3)) or {}
                )
                if not existing:
                    raise EvidenceIntegrityError(
                        "completed_followup_source_run_not_finalizable"
                    )
                event = existing[0]
                if (
                    len(existing) != 1
                    or canonical_value(
                        _json_value(_field(event, "payload", 0)) or {}
                    )
                    != record
                    or str(_field(event, "ref", 1) or "")
                    != f"completed-material-authority:{run_id}"
                    or str(_field(event, "run_id", 2) or "") != run_id
                    or str(_field(event, "thread_id", 3) or "")
                    != thread_id
                    or str(_field(event, "topic_id", 4) or "") != topic_id
                    or canonical_value(stored_request) != finalized_request
                ):
                    raise EvidenceIntegrityError(
                        "completed_followup_authority_record_conflict"
                    )
                self.connection.rollback()
                return finalized_request
            if run_status != "running_workflow":
                raise EvidenceIntegrityError(
                    "completed_followup_source_run_not_finalizable"
                )
            if existing:
                raise EvidenceIntegrityError(
                    "completed_followup_authority_record_conflict"
                )
            self._execute(
                """
                UPDATE waje_runtime.analysis_runs
                SET status = 'completed',
                    request = %(request)s::jsonb,
                    updated_at = now()
                WHERE run_id = %(run_id)s
                """,
                {"run_id": run_id, "request": _json(finalized_request)},
                commit=False,
            )
            self._audit(
                "run_status_changed",
                thread_id=thread_id,
                topic_id=topic_id,
                run_id=run_id,
                ref=run_id,
                payload={"status": "completed"},
                commit=False,
            )
            self._audit(
                "completed_material_authority_recorded",
                thread_id=thread_id,
                topic_id=topic_id,
                run_id=run_id,
                ref=f"completed-material-authority:{run_id}",
                payload=record,
                commit=False,
            )
            self.connection.commit()
            return finalized_request
        except Exception:
            self.connection.rollback()
            raise

    def record_context_manifest(self, manifest: dict[str, Any]) -> None:
        self.save_context_manifest(manifest)

    def save_context_manifest(self, manifest: ContextManifest | dict[str, Any]) -> None:
        payload = manifest.to_dict() if hasattr(manifest, "to_dict") else dict(manifest)
        self._execute(
            """
            INSERT INTO waje_runtime.context_manifests(
              manifest_id, thread_id, turn_id, can_support_claims, items
            )
            VALUES (
              %(manifest_id)s, %(thread_id)s, %(turn_id)s,
              %(can_support_claims)s, %(items)s::jsonb
            )
            ON CONFLICT (manifest_id) DO UPDATE
            SET can_support_claims = EXCLUDED.can_support_claims,
                items = EXCLUDED.items
            """,
            {
                "manifest_id": payload["manifest_id"],
                "thread_id": payload["thread_id"],
                "turn_id": payload.get("turn_id"),
                "can_support_claims": bool(payload.get("can_support_claims")),
                "items": _json(payload),
            },
        )
        self._audit(
            "context_manifest_recorded",
            thread_id=payload.get("thread_id"),
            ref=payload["manifest_id"],
        )

    def save_reuse_decisions(
        self,
        thread_id: str,
        turn_id: str,
        decisions: tuple[ReuseDecision, ...] | list[ReuseDecision],
    ) -> None:
        payload = [
            decision.to_dict() if hasattr(decision, "to_dict") else dict(decision)
            for decision in decisions
        ]
        self._audit(
            "reuse_decisions_recorded",
            thread_id=thread_id,
            ref=turn_id,
            payload={"turn_id": turn_id, "decisions": payload},
        )

    def list_context_manifests(self, thread_id: str) -> tuple[ContextManifest, ...]:
        rows = self._fetchall(
            """
            SELECT manifest_id, thread_id, turn_id, can_support_claims, items
            FROM waje_runtime.context_manifests
            WHERE thread_id = %(thread_id)s
            ORDER BY created_at
            """,
            {"thread_id": thread_id},
        )
        return tuple(_context_manifest_from_row(row) for row in rows)

    def record_answer_package(self, run_id: str, package: dict[str, Any]) -> None:
        artifact_id = package.get("artifact_id") or package.get("artifact_path") or f"answer-package:{run_id}"
        self._execute(
            """
            INSERT INTO waje_runtime.answer_packages(package_id, run_id, artifact_id, status, payload)
            VALUES (%(package_id)s, %(run_id)s, %(artifact_id)s, %(status)s, %(payload)s::jsonb)
            ON CONFLICT (package_id) DO UPDATE
            SET status = EXCLUDED.status,
                payload = EXCLUDED.payload
            """,
            {
                "package_id": f"answer-package:{run_id}",
                "run_id": run_id,
                "artifact_id": artifact_id,
                "status": package.get("status", "draft"),
                "payload": _json(package),
            },
        )
        run = self._fetchone(
            """
            SELECT thread_id, topic_id
            FROM waje_runtime.analysis_runs
            WHERE run_id = %(run_id)s
            """,
            {"run_id": run_id},
        )
        topic_id = _field(run, "topic_id", 1) if run else None
        if topic_id:
            self.add_artifact(
                artifact_id=str(artifact_id),
                topic_id=str(topic_id),
                follow_up_context=_follow_up_context(package),
                snapshot_id=str(package.get("snapshot_id") or package.get("snapshot") or "unknown"),
                permission_scope=str(package.get("permission_scope") or package.get("visibility") or "analyst"),
                run_id=run_id,
                payload=package,
            )
        self._audit("answer_package_recorded", run_id=run_id, ref=run_id)

    def runtime_evidence_resolver(self):
        from bi_agent.runtime.runtime_persistence import (
            PostgresRuntimeEvidenceResolver,
        )

        return PostgresRuntimeEvidenceResolver(self.connection)

    def save_analysis_runtime_records(
        self,
        *,
        run_id: str,
        analysis_contract: Mapping[str, Any],
        query_contracts: Sequence[Any],
        query_execution_records: Sequence[Any],
        rows_records: Sequence[Any],
        snapshot_records: Sequence[Any],
        completeness_records: Sequence[Any],
        capability_binding_records: Sequence[Any],
        evidence_manifests: Sequence[Mapping[str, Any]],
        context_manifests: Sequence[Mapping[str, Any]],
        trusted_provenance_records: Sequence[Mapping[str, Any]],
        verified_claims: Sequence[Mapping[str, Any]],
        claim_links: Sequence[Mapping[str, Any]],
        repair_attempts: Sequence[Mapping[str, Any]],
    ) -> str:
        from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
        from bi_agent.runtime.runtime_persistence import (
            authority_record_payload,
            validate_analysis_runtime_records,
        )

        bundle = validate_analysis_runtime_records(
            run_id=run_id,
            analysis_contract=analysis_contract,
            query_contracts=query_contracts,
            query_execution_records=query_execution_records,
            rows_records=rows_records,
            snapshot_records=snapshot_records,
            completeness_records=completeness_records,
            capability_binding_records=capability_binding_records,
            evidence_manifests=evidence_manifests,
            context_manifests=context_manifests,
            trusted_provenance_records=trusted_provenance_records,
            verified_claims=verified_claims,
            claim_links=claim_links,
            repair_attempts=repair_attempts,
        )
        analysis = bundle["analysis_contract"]
        bundle_digest = canonical_digest(bundle)
        context_owners = {
            (
                str(item["thread_id"]),
                str(item["topic_id"]),
            )
            for item in bundle["context_manifests"]
        }
        if len(context_owners) > 1:
            from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

            raise EvidenceIntegrityError("runtime_persistence_context_owner_mismatch")
        expected_thread_id, expected_topic_id = (
            next(iter(context_owners)) if context_owners else ("", "")
        )
        try:
            self._execute(
                """
                SELECT pg_advisory_xact_lock(
                  hashtextextended(%(lock_key)s, 0)
                )
                """,
                {"lock_key": f"analysis_runtime_publication:{run_id}"},
                commit=False,
            )
            publication = self._fetchone(
                """
                /* runtime_publication_preflight */
                SELECT r.run_id, r.thread_id, r.topic_id, p.bundle_digest
                FROM waje_runtime.analysis_runs r
                LEFT JOIN waje_runtime.analysis_runtime_publications p
                  ON p.run_id = r.run_id
                WHERE r.run_id = %(run_id)s
                FOR UPDATE OF r
                """,
                {
                    "run_id": run_id,
                    "expected_thread_id": expected_thread_id,
                    "expected_topic_id": expected_topic_id,
                },
            )
            if publication is None:
                from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

                raise EvidenceIntegrityError("analysis_runtime_run_missing")
            persisted_thread_id = str(_field(publication, "thread_id", 1) or "")
            persisted_topic_id = str(_field(publication, "topic_id", 2) or "")
            persisted_digest = str(_field(publication, "bundle_digest", 3) or "")
            if context_owners and (
                persisted_thread_id != expected_thread_id
                or persisted_topic_id != expected_topic_id
            ):
                from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

                raise EvidenceIntegrityError("analysis_runtime_owner_mismatch")
            if not context_owners:
                expected_thread_id = persisted_thread_id
                expected_topic_id = persisted_topic_id
            if persisted_digest:
                if persisted_digest == bundle_digest:
                    self.connection.rollback()
                    return "replayed"
                from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

                raise EvidenceIntegrityError("analysis_runtime_publication_conflict")
            self._insert_immutable(
                """
                INSERT INTO waje_runtime.analysis_contracts AS current(
                  analysis_contract_id, run_id, contract_signature, payload
                ) VALUES (
                  %(analysis_contract_id)s, %(run_id)s, %(contract_signature)s,
                  %(payload)s::jsonb
                )
                ON CONFLICT (analysis_contract_id) DO UPDATE
                SET analysis_contract_id = current.analysis_contract_id
                WHERE current.run_id = EXCLUDED.run_id
                  AND current.contract_signature = EXCLUDED.contract_signature
                  AND current.payload = EXCLUDED.payload
                RETURNING analysis_contract_id
                """,
                {
                    "analysis_contract_id": analysis["analysis_contract_id"],
                    "run_id": run_id,
                    "contract_signature": analysis["contract_signature"],
                    "payload": _json(analysis),
                },
                collision="analysis_contract",
            )
            for contract in bundle["query_contracts"]:
                payload = canonical_value(contract)
                self._insert_immutable(
                    """
                    INSERT INTO waje_runtime.query_contracts AS current(
                      query_contract_id, run_id, analysis_contract_id,
                      contract_signature, payload
                    ) VALUES (
                      %(query_contract_id)s, %(run_id)s, %(analysis_contract_id)s,
                      %(contract_signature)s, %(payload)s::jsonb
                    )
                    ON CONFLICT (query_contract_id) DO UPDATE
                    SET query_contract_id = current.query_contract_id
                    WHERE current.run_id = EXCLUDED.run_id
                      AND current.analysis_contract_id = EXCLUDED.analysis_contract_id
                      AND current.contract_signature = EXCLUDED.contract_signature
                      AND current.payload = EXCLUDED.payload
                    RETURNING query_contract_id
                    """,
                    {
                        "query_contract_id": contract.query_contract_id,
                        "run_id": run_id,
                        "analysis_contract_id": contract.analysis_contract_ref,
                        "contract_signature": contract.contract_signature,
                        "payload": _json(payload),
                    },
                    collision="query_contract",
                )
            for record in bundle["query_execution_records"]:
                self._insert_immutable(
                    """
                    INSERT INTO waje_runtime.query_runs AS current(
                      result_ref, run_id, query_contract_id, execution_status,
                      query_hash, rows_ref, completeness_report_ref, payload
                    ) VALUES (
                      %(result_ref)s, %(run_id)s, %(query_contract_id)s,
                      %(execution_status)s, %(query_hash)s, %(rows_ref)s,
                      %(completeness_report_ref)s, %(payload)s::jsonb
                    )
                    ON CONFLICT (result_ref) DO UPDATE
                    SET result_ref = current.result_ref
                    WHERE current.run_id = EXCLUDED.run_id
                      AND current.query_contract_id = EXCLUDED.query_contract_id
                      AND current.execution_status = EXCLUDED.execution_status
                      AND current.query_hash = EXCLUDED.query_hash
                      AND current.rows_ref = EXCLUDED.rows_ref
                      AND current.completeness_report_ref = EXCLUDED.completeness_report_ref
                      AND current.payload = EXCLUDED.payload
                    RETURNING result_ref
                    """,
                    {
                        "result_ref": record.result_ref,
                        "run_id": run_id,
                        "query_contract_id": record.query_contract_ref,
                        "execution_status": record.execution_status,
                        "query_hash": record.query_hash,
                        "rows_ref": record.rows_ref,
                        "completeness_report_ref": record.completeness_report_ref,
                        "payload": _json(canonical_value(record.result_payload)),
                    },
                    collision="query_run",
                )
            for record in bundle["snapshot_records"]:
                self._insert_authority_record(
                    table="snapshot_authority",
                    primary="record_ref",
                    columns={
                        "record_ref": record.record_ref,
                        "record_digest": record.record_digest,
                        "snapshot_ref": record.snapshot_ref,
                    },
                    payload=authority_record_payload("snapshot", record),
                    collision="snapshot_record",
                )
            for record in bundle["rows_records"]:
                self._insert_authority_record(
                    table="rows_metadata_authority",
                    primary="record_ref",
                    columns={
                        "record_ref": record.record_ref,
                        "record_digest": record.record_digest,
                        "rows_ref": record.rows_ref,
                        "rows_content_hash": record.rows_content_hash,
                        "row_count": record.row_count,
                        "unique_key_fields": _json(list(record.unique_key_fields)),
                        "storage_ref": record.storage_ref,
                    },
                    json_columns={"unique_key_fields"},
                    payload=authority_record_payload("rows", record),
                    collision="rows_record",
                )
            for record in bundle["query_execution_records"]:
                self._insert_authority_record(
                    table="query_execution_authority",
                    primary="record_ref",
                    columns={
                        "record_ref": record.record_ref,
                        "run_id": run_id,
                        "record_digest": record.record_digest,
                        "result_ref": record.result_ref,
                        "query_contract_ref": record.query_contract_ref,
                        "rows_ref": record.rows_ref,
                    },
                    payload=authority_record_payload("query_execution", record),
                    collision="query_execution_record",
                )
            for record in bundle["completeness_records"]:
                report = canonical_value(record.report_payload)
                self._insert_authority_record(
                    table="query_completeness_reports",
                    primary="record_ref",
                    columns={
                        "record_ref": record.record_ref,
                        "run_id": run_id,
                        "report_ref": record.report_ref,
                        "report_digest": record.report_digest,
                        "result_ref": record.result_ref,
                        "query_contract_ref": record.query_contract_ref,
                        "completeness_status": report["completeness_status"],
                        "analysis_readiness": report["analysis_readiness"],
                    },
                    payload=authority_record_payload("completeness", record),
                    collision="completeness_record",
                )
            for record in bundle["capability_binding_records"]:
                self._insert_authority_record(
                    table="capability_binding_authority",
                    primary="record_ref",
                    columns={
                        "record_ref": record.record_ref,
                        "run_id": run_id,
                        "binding_digest": record.binding_digest,
                        "capability_id": record.capability_id,
                        "analysis_contract_id": record.analysis_contract_ref,
                        "claim_strength_taxonomy_version": record.claim_strength_taxonomy_version,
                        "maximum_claim_strength_rank": record.maximum_claim_strength_rank,
                    },
                    payload=authority_record_payload("capability_binding", record),
                    collision="capability_binding_record",
                )
            for manifest in bundle["evidence_manifests"]:
                self._insert_immutable(
                    """
                    INSERT INTO waje_runtime.evidence_manifests AS current(
                      evidence_ref, run_id, binding_record_ref,
                      context_manifest_ref, payload
                    ) VALUES (
                      %(evidence_ref)s, %(run_id)s, %(binding_record_ref)s,
                      %(context_manifest_ref)s, %(payload)s::jsonb
                    )
                    ON CONFLICT (evidence_ref) DO UPDATE
                    SET evidence_ref = current.evidence_ref
                    WHERE current.run_id = EXCLUDED.run_id
                      AND current.binding_record_ref = EXCLUDED.binding_record_ref
                      AND current.context_manifest_ref = EXCLUDED.context_manifest_ref
                      AND current.payload = EXCLUDED.payload
                    RETURNING evidence_ref
                    """,
                    {
                        "evidence_ref": manifest["evidence_ref"],
                        "run_id": run_id,
                        "binding_record_ref": manifest["binding_record_ref"],
                        "context_manifest_ref": manifest["context_manifest_ref"],
                        "payload": _json(manifest),
                    },
                    collision="evidence_manifest",
                )
            for manifest in bundle["context_manifests"]:
                self._insert_immutable(
                    """
                    INSERT INTO waje_runtime.context_manifests AS current(
                      manifest_id, thread_id, turn_id, topic_id, run_id,
                      can_support_claims, items, manifest_digest, payload
                    ) VALUES (
                      %(manifest_id)s, %(thread_id)s, NULL, %(topic_id)s,
                      %(run_id)s, true, %(items)s::jsonb,
                      %(manifest_digest)s, %(payload)s::jsonb
                    )
                    ON CONFLICT (manifest_id) DO UPDATE
                    SET manifest_id = current.manifest_id
                    WHERE current.thread_id = EXCLUDED.thread_id
                      AND current.topic_id = EXCLUDED.topic_id
                      AND current.run_id = EXCLUDED.run_id
                      AND current.can_support_claims = EXCLUDED.can_support_claims
                      AND current.items = EXCLUDED.items
                      AND current.manifest_digest = EXCLUDED.manifest_digest
                      AND current.payload = EXCLUDED.payload
                    RETURNING manifest_id
                    """,
                    {
                        **manifest,
                        "items": _json(manifest["sources"]),
                        "payload": _json(manifest),
                    },
                    collision="context_manifest",
                )
            for provenance in bundle["trusted_provenance_records"]:
                self._insert_authority_record(
                    table="claim_provenance_records",
                    primary="record_ref",
                    columns={
                        "record_ref": provenance["record_ref"],
                        "run_id": run_id,
                        "record_digest": provenance["record_digest"],
                    },
                    payload=provenance,
                    collision="claim_provenance_record",
                )
            for claim in bundle["verified_claims"]:
                self._insert_authority_record(
                    table="verified_claims",
                    primary="claim_ref",
                    columns={
                        "claim_ref": claim["claim_ref"],
                        "run_id": run_id,
                        "context_manifest_ref": claim["context_manifest_ref"],
                        "provenance_record_ref": claim["provenance_record_ref"],
                        "claim_digest": claim["claim_digest"],
                    },
                    payload=claim,
                    collision="verified_claim",
                )
            for link in bundle["claim_links"]:
                self._insert_immutable(
                    """
                    INSERT INTO waje_runtime.claim_evidence_links AS current(
                      claim_ref, evidence_ref, context_manifest_ref, payload
                    ) VALUES (
                      %(claim_ref)s, %(evidence_ref)s,
                      %(context_manifest_ref)s, %(payload)s::jsonb
                    )
                    ON CONFLICT (claim_ref, evidence_ref) DO UPDATE
                    SET claim_ref = current.claim_ref
                    WHERE current.context_manifest_ref = EXCLUDED.context_manifest_ref
                      AND current.payload = EXCLUDED.payload
                    RETURNING claim_ref
                    """,
                    {**link, "payload": _json(link)},
                    collision="claim_evidence_link",
                )
            for repair in bundle["repair_attempts"]:
                self._insert_immutable(
                    """
                    INSERT INTO waje_runtime.query_repair_attempts AS current(
                      attempt_ref, run_id, failed_signature, action, reason, payload
                    ) VALUES (
                      %(attempt_ref)s, %(run_id)s, %(failed_signature)s,
                      %(action)s, %(reason)s, %(payload)s::jsonb
                    )
                    ON CONFLICT (attempt_ref) DO UPDATE
                    SET attempt_ref = current.attempt_ref
                    WHERE current.run_id = EXCLUDED.run_id
                      AND current.failed_signature = EXCLUDED.failed_signature
                      AND current.action = EXCLUDED.action
                      AND current.reason = EXCLUDED.reason
                      AND current.payload = EXCLUDED.payload
                    RETURNING attempt_ref
                    """,
                    {**repair, "run_id": run_id, "payload": _json(repair)},
                    collision="repair_attempt",
                )
            self._insert_immutable(
                """
                INSERT INTO waje_runtime.analysis_runtime_publications AS current(
                  run_id, analysis_contract_id, topic_id, bundle_digest, payload
                ) VALUES (
                  %(run_id)s, %(analysis_contract_id)s, %(topic_id)s,
                  %(bundle_digest)s, %(payload)s::jsonb
                )
                ON CONFLICT (run_id) DO UPDATE
                SET run_id = current.run_id
                WHERE current.analysis_contract_id = EXCLUDED.analysis_contract_id
                  AND current.topic_id = EXCLUDED.topic_id
                  AND current.bundle_digest = EXCLUDED.bundle_digest
                  AND current.payload = EXCLUDED.payload
                RETURNING run_id
                """,
                {
                    "run_id": run_id,
                    "analysis_contract_id": analysis["analysis_contract_id"],
                    "topic_id": expected_topic_id,
                    "bundle_digest": bundle_digest,
                    "payload": _json(_runtime_publication_index(bundle)),
                },
                collision="analysis_runtime_publication",
            )
            verification = self._fetchone(
                """
                /* runtime_publication_postcheck */
                SELECT p.bundle_digest,
                       count(DISTINCT q.query_contract_id) AS query_contract_count,
                       count(DISTINCT qr.result_ref) AS query_run_count,
                       count(DISTINCT qe.record_ref) AS query_authority_count,
                       count(DISTINCT c.record_ref) AS completeness_count,
                       count(DISTINCT b.record_ref) AS binding_count,
                       count(DISTINCT e.evidence_ref) AS evidence_count,
                       count(DISTINCT vc.claim_ref) AS verified_claim_count,
                       count(DISTINCT l.claim_ref || E'\\x1f' || l.evidence_ref) AS claim_link_count
                FROM waje_runtime.analysis_runtime_publications p
                JOIN waje_runtime.analysis_contracts a
                  ON a.analysis_contract_id = p.analysis_contract_id
                 AND a.run_id = p.run_id
                LEFT JOIN waje_runtime.query_contracts q ON q.run_id = p.run_id
                LEFT JOIN waje_runtime.query_runs qr ON qr.run_id = p.run_id
                LEFT JOIN waje_runtime.query_execution_authority qe ON qe.run_id = p.run_id
                LEFT JOIN waje_runtime.query_completeness_reports c ON c.run_id = p.run_id
                LEFT JOIN waje_runtime.capability_binding_authority b ON b.run_id = p.run_id
                LEFT JOIN waje_runtime.evidence_manifests e ON e.run_id = p.run_id
                LEFT JOIN waje_runtime.verified_claims vc ON vc.run_id = p.run_id
                LEFT JOIN waje_runtime.claim_evidence_links l ON l.claim_ref = vc.claim_ref
                WHERE p.run_id = %(run_id)s
                GROUP BY p.bundle_digest
                """,
                {
                    "run_id": run_id,
                    "expected_bundle_digest": bundle_digest,
                    "expected_query_contract_count": len(bundle["query_contracts"]),
                    "expected_query_run_count": len(bundle["query_execution_records"]),
                    "expected_query_authority_count": len(bundle["query_execution_records"]),
                    "expected_completeness_count": len(bundle["completeness_records"]),
                    "expected_binding_count": len(bundle["capability_binding_records"]),
                    "expected_evidence_count": len(bundle["evidence_manifests"]),
                    "expected_verified_claim_count": len(bundle["verified_claims"]),
                    "expected_claim_link_count": len(bundle["claim_links"]),
                },
            )
            expected_verification = (
                bundle_digest,
                len(bundle["query_contracts"]),
                len(bundle["query_execution_records"]),
                len(bundle["query_execution_records"]),
                len(bundle["completeness_records"]),
                len(bundle["capability_binding_records"]),
                len(bundle["evidence_manifests"]),
                len(bundle["verified_claims"]),
                len(bundle["claim_links"]),
            )
            actual_verification = tuple(
                _field(verification, name, index)
                for index, name in enumerate(
                    (
                        "bundle_digest",
                        "query_contract_count",
                        "query_run_count",
                        "query_authority_count",
                        "completeness_count",
                        "binding_count",
                        "evidence_count",
                        "verified_claim_count",
                        "claim_link_count",
                    )
                )
            ) if verification is not None else ()
            if actual_verification != expected_verification:
                from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

                raise EvidenceIntegrityError("analysis_runtime_postwrite_validation_failed")
            self._audit(
                "analysis_runtime_records_persisted",
                run_id=run_id,
                ref=str(analysis["analysis_contract_id"]),
                payload={
                    "bundle_digest": bundle_digest,
                    "query_count": len(bundle["query_execution_records"]),
                    "capability_binding_count": len(bundle["capability_binding_records"]),
                    "claim_link_count": len(bundle["claim_links"]),
                },
                commit=False,
            )
            self.connection.commit()
            return "published"
        except Exception:
            self.connection.rollback()
            raise

    def _insert_authority_record(
        self,
        *,
        table: str,
        primary: str,
        columns: Mapping[str, Any],
        payload: Mapping[str, Any],
        collision: str,
        json_columns: set[str] | None = None,
    ) -> None:
        json_columns = json_columns or set()
        names = [*columns, "payload"]
        value_sql = [
            f"%({name})s::jsonb" if name in json_columns or name == "payload" else f"%({name})s"
            for name in names
        ]
        where = " AND ".join(
            f"current.{name} = EXCLUDED.{name}" for name in names if name != primary
        )
        params = dict(columns)
        params["payload"] = _json(payload)
        self._insert_immutable(
            f"""
            INSERT INTO waje_runtime.{table} AS current({', '.join(names)})
            VALUES ({', '.join(value_sql)})
            ON CONFLICT ({primary}) DO UPDATE
            SET {primary} = current.{primary}
            WHERE {where}
            RETURNING {primary}
            """,
            params,
            collision=collision,
        )

    def _insert_immutable(
        self,
        statement: str,
        params: Mapping[str, Any],
        *,
        collision: str,
    ) -> None:
        cursor = self._execute(statement, dict(params), commit=False)
        if getattr(cursor, "rowcount", 1) != 1:
            from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

            raise EvidenceIntegrityError(f"authority_ref_collision:{collision}")

    def save_analysis_assets(
        self,
        thread_id: str,
        topic_id: str,
        assets: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    ) -> None:
        merged_assets = merge_analysis_assets(
            self.list_analysis_assets(thread_id, topic_id),
            assets,
        )
        self._execute(
            """
            DELETE FROM waje_runtime.analysis_assets
            WHERE thread_id = %(thread_id)s
              AND topic_id = %(topic_id)s
            """,
            {"thread_id": thread_id, "topic_id": topic_id},
            commit=False,
        )
        for index, asset in enumerate(merged_assets):
            payload = dict(asset)
            asset_id = str(
                payload.get("asset_id")
                or f"analysis-asset:{index:02d}:{asset_dedup_key(payload)[:16]}"
            )
            self._execute(
                """
                INSERT INTO waje_runtime.analysis_assets(
                  asset_id, thread_id, topic_id, asset_type, status, payload
                )
                VALUES (
                  %(asset_id)s, %(thread_id)s, %(topic_id)s, %(asset_type)s,
                  %(status)s, %(payload)s::jsonb
                )
                ON CONFLICT (asset_id) DO UPDATE
                SET asset_type = EXCLUDED.asset_type,
                    status = EXCLUDED.status,
                    payload = EXCLUDED.payload
                """,
                {
                    "asset_id": asset_id,
                    "thread_id": thread_id,
                    "topic_id": topic_id,
                    "asset_type": str(payload.get("asset_type") or "unknown"),
                    "status": str(payload.get("status") or "unknown"),
                    "payload": _json({**payload, "asset_id": asset_id}),
                },
                commit=False,
            )
        self._audit(
            "analysis_assets_recorded",
            thread_id=thread_id,
            topic_id=topic_id,
            ref=topic_id,
            payload={"count": len(merged_assets)},
            commit=False,
        )
        self.connection.commit()

    def list_analysis_assets(self, thread_id: str, topic_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._fetchall(
            """
            SELECT payload
            FROM waje_runtime.analysis_assets
            WHERE thread_id = %(thread_id)s
              AND topic_id = %(topic_id)s
            ORDER BY created_at, asset_id
            """,
            {"thread_id": thread_id, "topic_id": topic_id},
        )
        assets: list[dict[str, Any]] = []
        for row in rows:
            payload = _field(row, "payload", 0)
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = None
            if isinstance(payload, dict):
                assets.append(payload)
        return tuple(assets)

    def save_dataset_snapshot(self, payload: dict[str, Any]) -> None:
        dataset_id = str(payload.get("dataset_id") or "")
        snapshot_ref = str(payload.get("snapshot_ref") or "")
        requires_release = canonical_dataset_requires_release(dataset_id)
        try:
            self._execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
                {"lock_key": f"dataset_snapshot_member:{snapshot_ref}"},
                commit=False,
            )
            published = self._fetchone(
                """
                SELECT 1
                FROM waje_runtime.dataset_snapshot_releases r
                WHERE r.snapshot_refs @> to_jsonb(ARRAY[%(snapshot_ref)s::text])
                LIMIT 1
                """,
                {"snapshot_ref": snapshot_ref},
            )
            if published:
                raise ValueError("dataset_snapshot_published_immutable")
            if requires_release and payload.get("status") == "active":
                raise ValueError("dataset_snapshot_release_required")
            self._execute(
                """
                INSERT INTO waje_runtime.dataset_snapshots(
                  snapshot_ref, dataset_id, physical_table, watermark, schema_fingerprint,
                  schema_fields, contract_ref, permission_scopes, loaded_at, status,
                  logical_snapshot_id, load_revision, evidence_state,
                  reconciliation_status, reconciliation_ref, payload
                ) VALUES (
                  %(snapshot_ref)s, %(dataset_id)s, %(physical_table)s, %(watermark)s,
                  %(schema_fingerprint)s, %(schema_fields)s::jsonb, %(contract_ref)s,
                  %(permission_scopes)s::jsonb, %(loaded_at)s, %(status)s,
                  %(logical_snapshot_id)s, %(load_revision)s, %(evidence_state)s,
                  %(reconciliation_status)s, %(reconciliation_ref)s, %(payload)s::jsonb
                )
                ON CONFLICT (snapshot_ref) DO UPDATE SET
                  dataset_id = EXCLUDED.dataset_id,
                  physical_table = EXCLUDED.physical_table,
                  watermark = EXCLUDED.watermark,
                  schema_fingerprint = EXCLUDED.schema_fingerprint,
                  schema_fields = EXCLUDED.schema_fields,
                  contract_ref = EXCLUDED.contract_ref,
                  permission_scopes = EXCLUDED.permission_scopes,
                  loaded_at = EXCLUDED.loaded_at,
                  status = EXCLUDED.status,
                  logical_snapshot_id = EXCLUDED.logical_snapshot_id,
                  load_revision = EXCLUDED.load_revision,
                  evidence_state = EXCLUDED.evidence_state,
                  reconciliation_status = EXCLUDED.reconciliation_status,
                  reconciliation_ref = EXCLUDED.reconciliation_ref,
                  payload = EXCLUDED.payload
                """,
                {
                    **payload,
                    "logical_snapshot_id": payload.get("logical_snapshot_id", ""),
                    "load_revision": payload.get("load_revision", ""),
                    "evidence_state": payload.get("evidence_state", "claim_ready"),
                    "reconciliation_status": payload.get(
                        "reconciliation_status", "not_applicable"
                    ),
                    "reconciliation_ref": payload.get("reconciliation_ref", ""),
                    "schema_fields": _json(payload.get("schema_fields", [])),
                    "permission_scopes": _json(payload.get("permission_scopes", [])),
                    "payload": _json(payload),
                },
                commit=False,
            )
            self._audit(
                "dataset_snapshot_saved",
                ref=payload["snapshot_ref"],
                payload=payload,
                commit=False,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    @contextmanager
    def dataset_snapshot_release_lock(self, logical_snapshot_id: str):
        lock_key = f"dataset_snapshot_release:{logical_snapshot_id}"
        self._execute(
            "SELECT pg_advisory_lock(hashtextextended(%(lock_key)s, 0))",
            {"lock_key": lock_key},
            commit=False,
        )
        try:
            yield
        finally:
            self._execute(
                "SELECT pg_advisory_unlock(hashtextextended(%(lock_key)s, 0))",
                {"lock_key": lock_key},
                commit=False,
            )
            self.connection.commit()

    def publish_dataset_snapshot_release(
        self,
        *,
        release_ref: str,
        logical_snapshot_id: str,
        payloads: tuple[dict[str, Any], ...],
    ) -> None:
        normalized, validated_logical_id, _, validated_release_ref = (
            validate_dataset_snapshot_release_payloads(payloads)
        )
        if logical_snapshot_id != validated_logical_id:
            raise ValueError("dataset_snapshot_release_logical_snapshot")
        if release_ref != validated_release_ref:
            raise ValueError("dataset_snapshot_release_ref")
        authority = build_dataset_release_authority_record(normalized)
        if authority.integrity_errors:
            raise ValueError("dataset_release_authority_integrity")
        payloads = tuple(
            {
                **payload,
                "authority_record_ref": authority.authority_record_ref,
            }
            for payload in normalized
        )
        snapshot_refs = tuple(sorted(str(item["snapshot_ref"]) for item in payloads))
        try:
            for snapshot_ref in snapshot_refs:
                self._execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
                    {"lock_key": f"dataset_snapshot_member:{snapshot_ref}"},
                    commit=False,
                )
            self._execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
                {"lock_key": f"dataset_snapshot_release:{logical_snapshot_id}"},
                commit=False,
            )
            for payload in payloads:
                self._upsert_dataset_snapshot_in_transaction(payload)
            self._execute(
                """
                UPDATE waje_runtime.dataset_snapshots
                SET status = 'superseded',
                    payload = jsonb_set(
                      jsonb_set(payload, '{status}', '"superseded"'::jsonb),
                      '{superseded_by_release}', to_jsonb(%(release_ref)s::text)
                    )
                WHERE (
                    logical_snapshot_id = %(logical_snapshot_id)s
                    OR payload->>'snapshot_id' = %(logical_snapshot_id)s
                  )
                  AND status = 'active'
                  AND NOT (snapshot_ref = ANY(%(snapshot_refs)s))
                """,
                {
                    "logical_snapshot_id": logical_snapshot_id,
                    "release_ref": release_ref,
                    "snapshot_refs": list(snapshot_refs),
                },
                commit=False,
            )
            release_payload = authority.to_dict()
            self._execute(
                """
                INSERT INTO waje_runtime.dataset_snapshot_releases(
                  release_ref, logical_snapshot_id, load_revision, snapshot_refs, payload
                ) VALUES (
                  %(release_ref)s, %(logical_snapshot_id)s, %(load_revision)s,
                  %(snapshot_refs)s::jsonb, %(payload)s::jsonb
                )
                ON CONFLICT (release_ref) DO UPDATE
                SET payload = EXCLUDED.payload
                """,
                {
                    **release_payload,
                    "snapshot_refs": _json(snapshot_refs),
                    "payload": _json(release_payload),
                },
                commit=False,
            )
            self._audit(
                "dataset_snapshot_release_published",
                ref=release_ref,
                payload=release_payload,
                commit=False,
            )
            validation = self._fetchone(
                """
                WITH expected AS (
                  SELECT value AS payload
                  FROM jsonb_array_elements(%(expected_payloads)s::jsonb)
                )
                SELECT count(*) AS validated_count
                FROM expected e
                JOIN waje_runtime.dataset_snapshots s
                  ON s.snapshot_ref = e.payload->>'snapshot_ref'
                 AND s.dataset_id = e.payload->>'dataset_id'
                 AND s.physical_table = e.payload->>'physical_table'
                 AND s.watermark = (e.payload->>'watermark')::date
                 AND s.schema_fingerprint = e.payload->>'schema_fingerprint'
                 AND s.schema_fields = e.payload->'schema_fields'
                 AND s.contract_ref = e.payload->>'contract_ref'
                 AND s.permission_scopes = e.payload->'permission_scopes'
                 AND s.loaded_at = (e.payload->>'loaded_at')::timestamptz
                 AND s.status = e.payload->>'status'
                 AND s.logical_snapshot_id = e.payload->>'logical_snapshot_id'
                 AND s.load_revision = e.payload->>'load_revision'
                 AND s.evidence_state = e.payload->>'evidence_state'
                 AND s.reconciliation_status = e.payload->>'reconciliation_status'
                 AND s.reconciliation_ref = e.payload->>'reconciliation_ref'
                 AND s.payload = e.payload
                JOIN waje_runtime.dataset_snapshot_releases r
                  ON r.release_ref = e.payload->>'release_ref'
                 AND r.logical_snapshot_id = e.payload->>'logical_snapshot_id'
                 AND r.load_revision = e.payload->>'load_revision'
                 AND r.snapshot_refs = %(snapshot_refs)s::jsonb
                """,
                {
                    "expected_payloads": _json(payloads),
                    "snapshot_refs": _json(snapshot_refs),
                },
            )
            if int(_field(validation, "validated_count", 0) or 0) != len(payloads):
                raise RuntimeError("dataset_snapshot_release_validation_failed")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _upsert_dataset_snapshot_in_transaction(self, payload: dict[str, Any]) -> None:
        params = {
            **payload,
            "logical_snapshot_id": payload.get("logical_snapshot_id", ""),
            "load_revision": payload.get("load_revision", ""),
            "evidence_state": payload.get("evidence_state", "claim_ready"),
            "reconciliation_status": payload.get(
                "reconciliation_status", "not_applicable"
            ),
            "reconciliation_ref": payload.get("reconciliation_ref", ""),
            "schema_fields": _json(payload.get("schema_fields", [])),
            "permission_scopes": _json(payload.get("permission_scopes", [])),
            "payload": _json(payload),
        }
        self._execute(
            """
            INSERT INTO waje_runtime.dataset_snapshots(
              snapshot_ref, dataset_id, physical_table, watermark, schema_fingerprint,
              schema_fields, contract_ref, permission_scopes, loaded_at, status,
              logical_snapshot_id, load_revision, evidence_state,
              reconciliation_status, reconciliation_ref, payload
            ) VALUES (
              %(snapshot_ref)s, %(dataset_id)s, %(physical_table)s, %(watermark)s,
              %(schema_fingerprint)s, %(schema_fields)s::jsonb, %(contract_ref)s,
              %(permission_scopes)s::jsonb, %(loaded_at)s, %(status)s,
              %(logical_snapshot_id)s, %(load_revision)s, %(evidence_state)s,
              %(reconciliation_status)s, %(reconciliation_ref)s, %(payload)s::jsonb
            )
            ON CONFLICT (snapshot_ref) DO UPDATE SET
              payload = EXCLUDED.payload
            WHERE waje_runtime.dataset_snapshots.dataset_id = EXCLUDED.dataset_id
              AND waje_runtime.dataset_snapshots.physical_table = EXCLUDED.physical_table
              AND waje_runtime.dataset_snapshots.watermark = EXCLUDED.watermark
              AND waje_runtime.dataset_snapshots.schema_fingerprint = EXCLUDED.schema_fingerprint
              AND waje_runtime.dataset_snapshots.schema_fields = EXCLUDED.schema_fields
              AND waje_runtime.dataset_snapshots.contract_ref = EXCLUDED.contract_ref
              AND waje_runtime.dataset_snapshots.permission_scopes = EXCLUDED.permission_scopes
              AND waje_runtime.dataset_snapshots.loaded_at = EXCLUDED.loaded_at
              AND waje_runtime.dataset_snapshots.status = EXCLUDED.status
              AND waje_runtime.dataset_snapshots.logical_snapshot_id = EXCLUDED.logical_snapshot_id
              AND waje_runtime.dataset_snapshots.load_revision = EXCLUDED.load_revision
              AND waje_runtime.dataset_snapshots.evidence_state = EXCLUDED.evidence_state
              AND waje_runtime.dataset_snapshots.reconciliation_status = EXCLUDED.reconciliation_status
              AND waje_runtime.dataset_snapshots.reconciliation_ref = EXCLUDED.reconciliation_ref
              AND (
                waje_runtime.dataset_snapshots.payload - 'authority_record_ref'
                  - 'status' - 'superseded_by_release'
              ) = (
                EXCLUDED.payload - 'authority_record_ref'
                  - 'status' - 'superseded_by_release'
              )
            """,
            params,
            commit=False,
        )

    def list_dataset_snapshots(self, dataset_id: str = "") -> tuple[dict[str, Any], ...]:
        rows = self._fetchall(
            """
            SELECT s.payload
            FROM waje_runtime.dataset_snapshots s
            WHERE (%(dataset_id)s = '' OR s.dataset_id = %(dataset_id)s)
            ORDER BY s.loaded_at, s.snapshot_ref
            """,
            {"dataset_id": dataset_id},
        )
        snapshots: list[dict[str, Any]] = []
        for row in rows:
            payload = _field(row, "payload", 0)
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = None
            if isinstance(payload, dict):
                payload = dict(payload)
                snapshots.append(payload)
        return tuple(snapshots)

    def resolve_dataset_release(
        self,
        release_ref: str,
    ) -> DatasetReleaseAuthorityRecord:
        row = self._fetchone(
            """
            SELECT r.payload AS release_payload,
                   r.logical_snapshot_id,
                   r.load_revision,
                   r.snapshot_refs,
                   count(s.snapshot_ref) AS member_count,
                   jsonb_agg(s.payload ORDER BY s.snapshot_ref) AS member_payloads,
                   jsonb_agg(
                     jsonb_build_object(
                       'snapshot_ref', s.snapshot_ref,
                       'dataset_id', s.dataset_id,
                       'physical_table', s.physical_table,
                       'watermark', to_char(s.watermark, 'YYYY-MM-DD'),
                       'schema_fingerprint', s.schema_fingerprint,
                       'schema_fields', s.schema_fields,
                       'contract_ref', s.contract_ref,
                       'permission_scopes', s.permission_scopes,
                       'loaded_at', to_char(s.loaded_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"'),
                       'evidence_state', s.evidence_state,
                       'reconciliation_status', s.reconciliation_status,
                       'reconciliation_ref', s.reconciliation_ref,
                       'logical_snapshot_id', s.logical_snapshot_id,
                       'load_revision', s.load_revision
                     ) ORDER BY s.snapshot_ref
                   ) AS member_columns
            FROM waje_runtime.dataset_snapshot_releases r
            LEFT JOIN waje_runtime.dataset_snapshots s
              ON r.snapshot_refs @> to_jsonb(ARRAY[s.snapshot_ref])
            WHERE r.release_ref = %(release_ref)s
            GROUP BY r.release_ref, r.payload, r.logical_snapshot_id,
                     r.load_revision, r.snapshot_refs
            """,
            {"release_ref": release_ref},
        )
        if not row:
            raise KeyError(f"dataset_release_unavailable:{release_ref}")
        release_payload = _json_value(_field(row, "release_payload", 0))
        logical_snapshot_id = str(_field(row, "logical_snapshot_id", 1) or "")
        load_revision = str(_field(row, "load_revision", 2) or "")
        snapshot_refs = _json_value(_field(row, "snapshot_refs", 3))
        member_count = int(_field(row, "member_count", 4) or 0)
        member_payloads = _json_value(_field(row, "member_payloads", 5))
        member_columns = _json_value(_field(row, "member_columns", 6))
        stored = (
            dataset_release_authority_record_from_mapping(release_payload)
            if isinstance(release_payload, dict)
            else None
        )
        try:
            expected_count = len(
                canonical_dataset_release_members(stored.dataset_ids[0])
            ) if stored is not None else 0
        except (IndexError, KeyError, ValueError):
            expected_count = 0
        if (
            stored is None
            or not isinstance(snapshot_refs, list)
            or not isinstance(member_payloads, list)
            or not isinstance(member_columns, list)
            or not expected_count
            or member_count != expected_count
            or len(snapshot_refs) != expected_count
            or len(member_payloads) != expected_count
            or len(member_columns) != expected_count
            or tuple(str(item.get("snapshot_ref") or "") for item in member_payloads)
            != tuple(str(ref) for ref in snapshot_refs)
        ):
            raise ValueError("dataset_release_authority_membership")
        payload_projections = tuple(
            immutable_dataset_snapshot_projection(item)
            for item in member_payloads
        )
        mirrored_projections = tuple(
            immutable_dataset_snapshot_projection({**payload, **columns})
            for payload, columns in zip(member_payloads, member_columns)
        )
        if (
            stored.integrity_errors
            or stored.release_ref != release_ref
            or stored.logical_snapshot_id != logical_snapshot_id
            or stored.load_revision != load_revision
            or stored.snapshot_refs != tuple(str(ref) for ref in snapshot_refs)
            or payload_projections != stored.member_projections
            or mirrored_projections != stored.member_projections
        ):
            raise ValueError("dataset_release_authority_record_mismatch")
        return stored

    def record_run_nodes(self, run_id: str, checkpoint_events: tuple[dict, ...]) -> None:
        for index, event in enumerate(checkpoint_events):
            node_name = str(event.get("node") or event.get("name") or f"node_{index}")
            self._execute(
                """
                INSERT INTO waje_runtime.run_nodes(node_id, run_id, node_name, status, payload)
                VALUES (%(node_id)s, %(run_id)s, %(node_name)s, %(status)s, %(payload)s::jsonb)
                ON CONFLICT (node_id) DO UPDATE
                SET status = EXCLUDED.status,
                    payload = EXCLUDED.payload,
                    finished_at = now()
                """,
                {
                    "node_id": f"{run_id}:{index}:{node_name}",
                    "run_id": run_id,
                    "node_name": node_name,
                    "status": str(event.get("status") or "completed"),
                    "payload": _json(event),
                },
            )
        self._audit("run_nodes_recorded", run_id=run_id, ref=run_id, payload={"count": len(checkpoint_events)})

    def add_audit_event(
        self,
        event_type: str,
        *,
        thread_id: str = "",
        topic_id: str = "",
        run_id: str = "",
        ref: str = "",
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        self._audit(
            event_type,
            thread_id=thread_id or None,
            topic_id=topic_id or None,
            run_id=run_id or None,
            ref=ref or None,
            payload=payload or {},
        )

    def add_result_ref(
        self,
        topic_id: str,
        *,
        result_ref: str,
        snapshot_id: str,
        contract_version: str,
        permission_scope: str,
        semantic_scope: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        candidate_payload = (
            validate_result_reuse_candidate(payload) if payload else {}
        )
        if candidate_payload and (
            candidate_payload["result_ref"] != result_ref
            or candidate_payload["runtime_snapshot_id"] != snapshot_id
            or candidate_payload["runtime_contract_version"] != contract_version
            or candidate_payload["permission_scope"] != permission_scope
            or candidate_payload["semantic_scope_signature"] != semantic_scope
        ):
            raise EvidenceIntegrityError("result_ref_candidate_projection_mismatch")
        params = {
            "result_ref": result_ref,
            "topic_id": topic_id,
            "snapshot_id": snapshot_id,
            "contract_version": contract_version,
            "permission_scope": permission_scope,
            "semantic_scope": semantic_scope,
            "payload": _json(candidate_payload),
        }
        try:
            row = self.connection.execute(
                """
                /* result_ref_immutable_write */
                INSERT INTO waje_runtime.result_refs AS current(
                  result_ref, topic_id, snapshot_id, contract_version,
                  permission_scope, semantic_scope, payload
                )
                VALUES (
                  %(result_ref)s, %(topic_id)s, %(snapshot_id)s,
                  %(contract_version)s, %(permission_scope)s,
                  %(semantic_scope)s, %(payload)s::jsonb
                )
                ON CONFLICT (result_ref) DO UPDATE
                SET result_ref = current.result_ref
                WHERE current.topic_id = EXCLUDED.topic_id
                  AND current.snapshot_id = EXCLUDED.snapshot_id
                  AND current.contract_version = EXCLUDED.contract_version
                  AND current.permission_scope = EXCLUDED.permission_scope
                  AND current.semantic_scope = EXCLUDED.semantic_scope
                  AND current.payload = EXCLUDED.payload
                RETURNING result_ref
                """,
                params,
            ).fetchone()
            if row is None:
                raise EvidenceIntegrityError("result_ref_payload_conflict")
            self._audit(
                "result_ref_recorded",
                topic_id=topic_id,
                run_id=str(candidate_payload.get("source_run_id") or ""),
                ref=result_ref,
                payload={"candidate_signature": candidate_payload.get("candidate_signature")},
            )
        except Exception:
            self.connection.rollback()
            raise

    def results_for_topic(self, topic_id: Optional[str]) -> tuple[ResultRefRecord, ...]:
        if not topic_id:
            return ()
        rows = self._fetchall(
            """
            SELECT topic_id, result_ref, snapshot_id, contract_version,
                   permission_scope, semantic_scope, payload
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
                payload=_json_value(_field(row, "payload", 6)) or {},
            )
            for row in rows
        )

    def resolve_result_candidate_authority(
        self,
        *,
        result_ref: str,
        topic_id: str,
    ) -> dict[str, Any]:
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError, canonical_value

        row = self._fetchone(
            """
            /* result_candidate_authority */
            SELECT rr.topic_id,
                   rr.result_ref,
                   rr.snapshot_id,
                   rr.contract_version,
                   rr.permission_scope,
                   rr.semantic_scope,
                   rr.payload AS result_ref_payload,
                   r.run_id AS source_run_id,
                   r.thread_id AS run_thread_id,
                   r.topic_id AS run_topic_id,
                   r.status AS run_status,
                   r.request AS source_run_request,
                   ac.payload AS analysis_contract,
                   ac.contract_signature AS stored_analysis_contract_signature
            FROM waje_runtime.result_refs rr
            JOIN waje_runtime.analysis_runs r
              ON r.run_id = rr.payload->>'source_run_id'
             AND r.topic_id = rr.topic_id
            JOIN waje_runtime.analysis_contracts ac
              ON ac.run_id = r.run_id
             AND ac.analysis_contract_id = rr.payload->>'analysis_contract_ref'
            WHERE rr.result_ref = %(result_ref)s
              AND rr.topic_id = %(topic_id)s
            """,
            {"result_ref": result_ref, "topic_id": topic_id},
        )
        if row is None:
            raise EvidenceIntegrityError("result_candidate_authority_missing")
        payload = validate_result_reuse_candidate(
            _json_value(_field(row, "result_ref_payload", 6)) or {}
        )
        contract = _json_value(_field(row, "analysis_contract", 12)) or {}
        stored_signature = str(
            _field(row, "stored_analysis_contract_signature", 13) or ""
        )
        if (
            payload["source_run_id"] != str(_field(row, "source_run_id", 7) or "")
            or payload["analysis_contract_signature"] != stored_signature
            or analysis_contract_signature(contract) != stored_signature
        ):
            raise EvidenceIntegrityError("result_candidate_analysis_contract_mismatch")
        record = ResultRefRecord(
            topic_id=str(_field(row, "topic_id", 0) or ""),
            result_ref=str(_field(row, "result_ref", 1) or ""),
            snapshot_id=str(_field(row, "snapshot_id", 2) or ""),
            contract_version=str(_field(row, "contract_version", 3) or ""),
            permission_scope=str(_field(row, "permission_scope", 4) or ""),
            semantic_scope=str(_field(row, "semantic_scope", 5) or ""),
            payload=payload,
        )
        return canonical_value(
            {
                "result_ref_record": record.to_dict(),
                "source_run_id": payload["source_run_id"],
                "run_thread_id": str(_field(row, "run_thread_id", 8) or ""),
                "run_topic_id": str(_field(row, "run_topic_id", 9) or ""),
                "run_status": str(_field(row, "run_status", 10) or ""),
                "source_run_request": _json_value(
                    _field(row, "source_run_request", 11)
                )
                or {},
                "analysis_contract": contract,
                "stored_analysis_contract_signature": stored_signature,
            }
        )

    def add_artifact(
        self,
        *,
        artifact_id: str,
        topic_id: str,
        follow_up_context: str,
        snapshot_id: str,
        permission_scope: str,
        run_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        self._execute(
            """
            INSERT INTO waje_runtime.investigation_artifacts(
              artifact_id, thread_id, topic_id, run_id, snapshot_id, permission_scope, follow_up_context, payload
            )
            SELECT
              %(artifact_id)s, thread_id, topic_id, %(run_id)s, %(snapshot_id)s,
              %(permission_scope)s, %(follow_up_context)s, %(payload)s::jsonb
            FROM waje_runtime.conversation_topics
            WHERE topic_id = %(topic_id)s
            ON CONFLICT (artifact_id) DO UPDATE
            SET snapshot_id = EXCLUDED.snapshot_id,
                permission_scope = EXCLUDED.permission_scope,
                follow_up_context = EXCLUDED.follow_up_context,
                payload = EXCLUDED.payload
            """,
            {
                "artifact_id": artifact_id,
                "topic_id": topic_id,
                "run_id": run_id,
                "snapshot_id": snapshot_id,
                "permission_scope": permission_scope,
                "follow_up_context": follow_up_context,
                "payload": _json(payload or {}),
            },
        )
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
        if not topic_id:
            return None
        row = self._fetchone(
            """
            SELECT artifact_id, topic_id, follow_up_context, snapshot_id, permission_scope
            FROM waje_runtime.investigation_artifacts
            WHERE topic_id = %(topic_id)s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {"topic_id": topic_id},
        )
        if not row:
            return None
        return ArtifactRef(
            artifact_id=_field(row, "artifact_id", 0),
            topic_id=_field(row, "topic_id", 1),
            follow_up_context=_field(row, "follow_up_context", 2),
            snapshot_id=_field(row, "snapshot_id", 3),
            permission_scope=_field(row, "permission_scope", 4),
        )

    def add_memory_item(
        self,
        *,
        owner_scope: str,
        text: str,
        source_ref: str,
        visibility: str,
        status: str,
        refresh_rule: str = "refresh_on_contract_or_scope_change",
        revocation_path: str = "memory_proposal_revoke_or_admin_action",
    ) -> MemoryItem:
        item = MemoryItem(
            memory_id=f"memory-{uuid4().hex[:12]}",
            owner_scope=owner_scope,
            text=text,
            source_ref=source_ref,
            visibility=visibility,
            status=status,
            refresh_rule=refresh_rule,
            revocation_path=revocation_path,
        )
        self._execute(
            """
            INSERT INTO waje_runtime.memory_items(
              memory_id, owner_scope, text, source_ref, visibility, status,
              ttl, confidence, refresh_rule, revocation_path
            )
            VALUES (
              %(memory_id)s, %(owner_scope)s, %(text)s, %(source_ref)s,
              %(visibility)s, %(status)s, %(ttl)s, %(confidence)s,
              %(refresh_rule)s, %(revocation_path)s
            )
            """,
            item.__dict__,
        )
        self._audit("memory_item_recorded", ref=item.memory_id, payload={"owner_scope": owner_scope})
        return item

    def long_term_memory(self, owner_scope: str) -> tuple[MemoryItem, ...]:
        rows = self._fetchall(
            """
            SELECT memory_id, owner_scope, text, source_ref, visibility, status,
                   ttl, confidence, refresh_rule, revocation_path
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
                refresh_rule=_field(row, "refresh_rule", 8),
                revocation_path=_field(row, "revocation_path", 9),
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

    def _execute(
        self,
        statement: str,
        params: Optional[dict[str, Any]] = None,
        *,
        commit: bool = True,
    ) -> Any:
        result = self.connection.execute(statement, params or {})
        if commit:
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
        commit: bool = True,
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
            commit=commit,
        )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _field(row: Any, key: str, index: int) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[index]
    except (IndexError, TypeError):
        return None


def _context_manifest_from_row(row: Any) -> ContextManifest:
    payload = _field(row, "items", 4)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    if isinstance(payload, list):
        payload = {"items": payload}
    if not isinstance(payload, dict):
        payload = {}
    manifest_id = payload.get("manifest_id") or _field(row, "manifest_id", 0)
    thread_id = payload.get("thread_id") or _field(row, "thread_id", 1)
    return ContextManifest(
        manifest_id=manifest_id,
        thread_id=thread_id,
        turn_id=payload.get("turn_id") or _field(row, "turn_id", 2) or "",
        topic_id=payload.get("topic_id"),
        items=tuple(
            ContextItem(
                source_type=item.get("source_type") or item.get("type", ""),
                source_ref=item.get("source_ref") or item.get("ref", ""),
                summary=item.get("summary", ""),
                can_support_claims=bool(item.get("can_support_claims")),
                visibility=item.get("visibility", "analyst"),
                reason=item.get("reason", ""),
                permission_scope=item.get("permission_scope", ""),
                source_version=item.get("source_version", ""),
                expired=bool(item.get("expired")),
                claim_use=item.get("claim_use", "context_only"),
            )
            for item in payload.get("items", ())
            if isinstance(item, dict)
        ),
        sources=list(payload.get("sources") or []),
        claim_use_policy=dict(payload.get("claim_use_policy") or {}),
        snapshot_version=payload.get("snapshot_version"),
        permission_context=dict(payload.get("permission_context") or {}),
        analysis_assets=list(payload.get("analysis_assets") or []),
        accepted_assumptions=list(payload.get("accepted_assumptions") or []),
        contract_versions=dict(payload.get("contract_versions") or {}),
        schema_fingerprint=str(payload.get("schema_fingerprint") or ""),
        created_at=payload.get("created_at"),
        can_support_claims=bool(payload.get("can_support_claims", _field(row, "can_support_claims", 3))),
    )


def _clarification_state_from_payload(payload: Any) -> Optional[ClarificationState]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    options = []
    for item in payload.get("options") or []:
        if isinstance(item, dict):
            options.append(
                ClarificationOption(
                    option_id=item.get("option_id") or item.get("id"),
                    label=str(item.get("label") or ""),
                    description=item.get("description") or item.get("business_meaning") or "",
                    recommended=bool(item.get("recommended")),
                )
            )
    return ClarificationState(
        run_id=str(payload.get("run_id") or ""),
        topic_id=str(payload.get("topic_id") or ""),
        question=str(payload.get("question") or ""),
        options=options,
        status=str(payload.get("status") or "waiting"),
        answer=payload.get("answer"),
    )


def _follow_up_context(package: dict[str, Any]) -> str:
    for key in ("follow_up_context", "business_summary", "final_answer", "answer", "summary"):
        value = package.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "已验证 Answer Package，可作为继续调查上下文。"
