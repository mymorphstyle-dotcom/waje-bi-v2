from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from uuid import uuid4

from bi_agent.conversation.models import (
    CLARIFICATION_ESCAPE_OPTION,
    ClarificationOption,
    ClarificationState,
    ContextItem,
    ContextManifest,
    MemoryItem,
    MemoryProposal,
    ThreadState,
    TopicState,
)
from bi_agent.conversation.run_status import (
    validate_run_status_transition,
    validate_run_status_value,
)
from bi_agent.conversation.material_revision_continuation import (
    MaterialRevisionContinuation,
)
from bi_agent.runtime.dataset_catalog import (
    DatasetReleaseAuthorityRecord,
    build_dataset_release_authority_record,
    canonical_dataset_release_members,
    canonical_dataset_requires_release,
    dataset_release_authority_record_from_mapping,
    immutable_dataset_snapshot_projection,
    validate_dataset_snapshot_release_payloads,
)
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError
from bi_agent.runtime.plan_authority import (
    AuthorityContext,
    PlanRevision,
    PlannerProposal,
    ProposalAdmissionRecord,
)
from bi_agent.runtime.single_authority import (
    DecisionLedger,
    DecisionRecord,
    DurableTransition,
    FailureRecord,
    InteractionDirective,
    IntentRevision,
    LifecycleState,
)
from bi_agent.runtime.temporal_comparison import (
    normalize_temporal_decision_value,
    temporal_decision_option_id,
)


ROOT = Path(__file__).resolve().parents[2]
CONVERSATION_SCHEMA_SQL = (
    ROOT / "tools" / "runtime" / "conversation-runtime.sql"
).read_text(encoding="utf-8")


class PostgresConversationStore:
    def __init__(self, connection: Any) -> None:
        from bi_agent.runtime.durable_call_journal import (
            PostgresDurableCallJournal,
        )

        self.connection = connection
        self.attempt_journal = PostgresDurableCallJournal(connection)
        from bi_agent.runtime.thread_item_ledger import PostgresThreadItemLedger

        self.thread_item_ledger = PostgresThreadItemLedger(connection)
        self._actor_id = "system"
        self._active_run_dispatches: dict[str, tuple[str, str, int]] = {}
        self._run_dispatch_heartbeat_stops: dict[str, threading.Event] = {}
        self._conversation_entry_locks_guard = threading.RLock()
        self._conversation_entry_locks: dict[str, threading.RLock] = {}

    def set_actor_id(self, actor_id: str) -> None:
        self._actor_id = actor_id or "system"
        self.connection.execute(
            "SELECT set_config('waje.actor_id', %(actor_id)s, false)",
            {"actor_id": self._actor_id},
        )
        self.connection.commit()

    @classmethod
    def from_env(cls) -> "PostgresConversationStore":
        dsn = os.environ.get("WAJE_RUNTIME_DATABASE_URL") or os.environ.get(
            "DATABASE_URL"
        )
        if not dsn:
            raise RuntimeError("WAJE_RUNTIME_DATABASE_URL or DATABASE_URL is required")
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "psycopg is required for PostgresConversationStore"
            ) from exc
        return cls(psycopg.connect(dsn, options="-c waje.actor_id=system"))

    def apply_schema(self) -> None:
        self.connection.execute(CONVERSATION_SCHEMA_SQL)
        self.connection.commit()

    def recover_after_write_failure(self) -> None:
        self.connection.rollback()

    def create_thread(
        self, thread_id: Optional[str] = None, *, owner_id: str = "user"
    ) -> ThreadState:
        thread_id = thread_id or f"thread-{uuid4().hex[:12]}"
        self._execute(
            """
            INSERT INTO waje_runtime.investigation_threads(thread_id, owner_id)
            VALUES (%(thread_id)s, %(owner_id)s)
            ON CONFLICT (thread_id) DO NOTHING
            RETURNING thread_id, owner_id
            """,
            {"thread_id": thread_id, "owner_id": owner_id},
        )
        row = self.connection.execute(
            """
            SELECT thread_id, owner_id
            FROM waje_runtime.investigation_threads
            WHERE thread_id = %(thread_id)s
            """,
            {"thread_id": thread_id},
        ).fetchone()
        if row is None:
            raise EvidenceIntegrityError("thread_creation_not_visible")
        persisted_owner = str(_field(row, "owner_id", 1))
        if persisted_owner != owner_id:
            raise EvidenceIntegrityError("thread_owner_immutable")
        self._audit(
            "thread_created",
            thread_id=thread_id,
            ref=thread_id,
            payload={"owner_id": owner_id},
        )
        return ThreadState(thread_id=thread_id, owner_id=persisted_owner)

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
            pending_clarification_topic_id=_field(
                row, "pending_clarification_topic_id", 3
            ),
            pending_clarification_id=_field(row, "pending_clarification_id", 4) or "",
        )

    def create_topic(
        self, thread_id: str, *, title: str, summary: str = ""
    ) -> TopicState:
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
        self._audit(
            "topic_created", thread_id=thread_id, topic_id=topic_id, ref=topic_id
        )
        return TopicState(
            topic_id=topic_id,
            thread_id=thread_id,
            title=title,
            summary=summary or title,
        )

    def set_current_topic(self, thread_id: str, topic_id: str) -> None:
        self._execute(
            """
            UPDATE waje_runtime.investigation_threads
            SET current_topic_id = %(topic_id)s, updated_at = now()
            WHERE thread_id = %(thread_id)s
            """,
            {"thread_id": thread_id, "topic_id": topic_id},
        )
        self._audit(
            "current_topic_set", thread_id=thread_id, topic_id=topic_id, ref=topic_id
        )

    @contextmanager
    def conversation_entry_lock(self, run_attempt_id: str):
        if not isinstance(run_attempt_id, str) or not run_attempt_id.strip():
            raise ValueError("conversation_entry_run_attempt_id_invalid")
        with self._conversation_entry_locks_guard:
            local_lock = self._conversation_entry_locks.setdefault(
                run_attempt_id,
                threading.RLock(),
            )
        with local_lock:
            lock_key = f"conversation-entry:{run_attempt_id}"
            self.connection.execute(
                "SELECT pg_advisory_lock(hashtextextended(%(lock_key)s, 0))",
                {"lock_key": lock_key},
            )
            self.connection.commit()
            try:
                yield
            finally:
                try:
                    row = self.connection.execute(
                        """
                        SELECT pg_advisory_unlock(
                          hashtextextended(%(lock_key)s, 0)
                        ) AS unlocked
                        """,
                        {"lock_key": lock_key},
                    ).fetchone()
                    if row is None or _field(row, "unlocked", 0) is not True:
                        raise RuntimeError("conversation_entry_advisory_unlock_failed")
                    self.connection.commit()
                except Exception:
                    self.connection.rollback()
                    raise

    def claim_conversation_entry_command(
        self,
        *,
        run_attempt_id: str,
        thread_id: str,
        command_payload: Mapping[str, Any],
        orchestration_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        from datetime import datetime, timezone

        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
            canonical_value,
        )

        command = canonical_value(command_payload)
        provider_input = canonical_value(orchestration_input)
        if not isinstance(command, dict) or not isinstance(provider_input, dict):
            raise EvidenceIntegrityError("conversation_entry_command_invalid")
        state = canonical_value(
            {
                "schema_version": "conversation-entry-command.v1",
                "run_attempt_id": run_attempt_id,
                "thread_id": thread_id,
                "command_digest": canonical_digest(command),
                "claimed_at": datetime.now(timezone.utc).isoformat(),
                "command_payload": command,
                "orchestration_input": provider_input,
            }
        )
        try:
            row = self.connection.execute(
                """
                SELECT thread_id, request
                FROM waje_runtime.analysis_runs
                WHERE run_id = %(run_attempt_id)s
                  AND run_attempt_id = %(run_attempt_id)s
                FOR UPDATE
                """,
                {"run_attempt_id": run_attempt_id},
            ).fetchone()
            if row is None:
                raise EvidenceIntegrityError("conversation_entry_run_missing")
            if str(_field(row, "thread_id", 0) or "") != thread_id:
                raise EvidenceIntegrityError("conversation_entry_run_owner_mismatch")
            request = _json_value(_field(row, "request", 1)) or {}
            if not isinstance(request, Mapping):
                raise EvidenceIntegrityError("conversation_entry_request_invalid")
            existing = request.get("conversation_entry")
            if existing is not None:
                if not isinstance(existing, Mapping):
                    raise EvidenceIntegrityError("conversation_entry_command_invalid")
                existing_base = {
                    key: value
                    for key, value in existing.items()
                    if key
                    not in {
                        "accepted_attempt_ref",
                        "accepted_output_digest",
                        "transition_attempt_id",
                    }
                }
                if canonical_value(existing_base.get("command_payload")) != command:
                    raise EvidenceIntegrityError("conversation_entry_command_conflict")
                self.connection.commit()
                return canonical_value(existing_base)
            updated = canonical_value({**dict(request), "conversation_entry": state})
            cursor = self.connection.execute(
                """
                UPDATE waje_runtime.analysis_runs
                SET request = %(request)s::jsonb, updated_at = now()
                WHERE run_id = %(run_attempt_id)s
                  AND run_attempt_id = %(run_attempt_id)s
                  AND thread_id = %(thread_id)s
                """,
                {
                    "run_attempt_id": run_attempt_id,
                    "thread_id": thread_id,
                    "request": _json(updated),
                },
            )
            if getattr(cursor, "rowcount", 1) != 1:
                raise EvidenceIntegrityError("conversation_entry_command_conflict")
            self.connection.commit()
            return state
        except Exception:
            self.connection.rollback()
            raise

    def accept_conversation_entry(
        self,
        *,
        run_attempt_id: str,
        command_state: Mapping[str, Any],
        call_spec: Any,
        accepted_attempt_ref: str,
        orchestration: Mapping[str, Any],
        transition: Any,
        topic: TopicState | None,
        topic_is_new: bool,
        set_current_topic: bool,
        turn: Mapping[str, Any],
        manifest: ContextManifest,
    ) -> str:
        from bi_agent.conversation.entry_authority import (
            CONVERSATION_ENTRY_STAGE,
            build_conversation_entry_transition,
        )
        from bi_agent.runtime.durable_call_journal import (
            DurableCallJournalError,
            DurableCallSpec,
        )
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )
        from bi_agent.runtime.single_authority import DurableTransition

        if type(call_spec) is not DurableCallSpec:
            raise EvidenceIntegrityError("conversation_entry_acceptance_invalid")
        call_spec = DurableCallSpec.from_dict(call_spec.to_dict())
        state = canonical_value(command_state)
        turn_payload = canonical_value(turn)
        manifest_payload = canonical_value(manifest.to_dict())
        thread_id = str(state.get("thread_id") or "")
        turn_id = str(turn_payload.get("turn_id") or "")
        manifest_id = str(manifest_payload.get("manifest_id") or "")
        topic_id = topic.topic_id if topic is not None else ""
        if (
            call_spec.run_attempt_id != run_attempt_id
            or not isinstance(accepted_attempt_ref, str)
            or not accepted_attempt_ref.strip()
            or not thread_id
            or not turn_id
            or not manifest_id
            or str(turn_payload.get("thread_id") or "") != thread_id
            or str(manifest_payload.get("thread_id") or "") != thread_id
            or str(manifest_payload.get("run_id") or "") != run_attempt_id
        ):
            raise EvidenceIntegrityError("conversation_entry_acceptance_invalid")
        try:
            run_row = self.connection.execute(
                """
                SELECT thread_id, turn_id, topic_id, request
                FROM waje_runtime.analysis_runs
                WHERE run_id = %(run_attempt_id)s
                  AND run_attempt_id = %(run_attempt_id)s
                FOR UPDATE
                """,
                {"run_attempt_id": run_attempt_id},
            ).fetchone()
            if (
                run_row is None
                or str(_field(run_row, "thread_id", 0) or "") != thread_id
            ):
                raise EvidenceIntegrityError("conversation_entry_run_owner_mismatch")
            request = _json_value(_field(run_row, "request", 3)) or {}
            entry = (
                request.get("conversation_entry")
                if isinstance(request, Mapping)
                else None
            )
            if not isinstance(entry, Mapping):
                raise EvidenceIntegrityError("conversation_entry_command_missing")
            entry_base = {
                key: value
                for key, value in entry.items()
                if key
                not in {
                    "accepted_attempt_ref",
                    "accepted_output_digest",
                    "transition_attempt_id",
                }
            }
            if canonical_value(entry_base) != state:
                raise EvidenceIntegrityError("conversation_entry_command_conflict")
            stored_accepted_attempt_ref = str(entry.get("accepted_attempt_ref") or "")
            if stored_accepted_attempt_ref not in {"", accepted_attempt_ref}:
                raise EvidenceIntegrityError("conversation_entry_binding_conflict")
            try:
                accepted_call = self.attempt_journal.load_accepted_call(
                    call_spec=call_spec,
                    accepted_attempt_ref=accepted_attempt_ref,
                )
                supplied_transition = DurableTransition.from_dict(transition.to_dict())
                expected_transition, transition_input, transition_output = (
                    build_conversation_entry_transition(
                        run_attempt_id=run_attempt_id,
                        command_state=state,
                        call_spec=accepted_call.attempt.spec,
                        accepted_attempt_ref=accepted_attempt_ref,
                        accepted_output_payload=(
                            accepted_call.acceptance.output_payload
                        ),
                        orchestration=orchestration,
                        topic=topic,
                        topic_is_new=topic_is_new,
                        set_current_topic=set_current_topic,
                        turn=turn_payload,
                        manifest=manifest,
                    )
                )
            except DurableCallJournalError as exc:
                raise EvidenceIntegrityError(
                    "conversation_entry_acceptance_invalid"
                ) from exc
            except (AttributeError, TypeError, ValueError) as exc:
                raise EvidenceIntegrityError(
                    "conversation_entry_transition_invalid"
                ) from exc
            if supplied_transition != expected_transition:
                raise EvidenceIntegrityError("conversation_entry_transition_invalid")
            self._save_transition_attempt_locked(
                transition=expected_transition,
                input_payload=transition_input,
                output_payload=transition_output,
            )
            self.attempt_journal.bind_stage(
                run_attempt_id=run_attempt_id,
                transition_attempt_id=expected_transition.attempt_id,
                stage_name=CONVERSATION_ENTRY_STAGE,
                attempt_refs=(accepted_attempt_ref,),
                commit=False,
            )

            existing_turn = self.connection.execute(
                """
                SELECT thread_id, topic_id, intent, payload
                FROM waje_runtime.conversation_turns
                WHERE turn_id = %(turn_id)s
                FOR UPDATE
                """,
                {"turn_id": turn_id},
            ).fetchone()
            existing_manifest = self.connection.execute(
                """
                SELECT thread_id, turn_id, topic_id, run_id,
                       can_support_claims, items, manifest_digest, payload
                FROM waje_runtime.context_manifests
                WHERE manifest_id = %(manifest_id)s
                FOR UPDATE
                """,
                {"manifest_id": manifest_id},
            ).fetchone()
            if existing_turn is not None or existing_manifest is not None:
                if not self._conversation_entry_rows_match(
                    existing_turn=existing_turn,
                    existing_manifest=existing_manifest,
                    thread_id=thread_id,
                    topic_id=topic_id,
                    turn_payload=turn_payload,
                    manifest_payload=manifest_payload,
                ) or (
                    str(_field(run_row, "turn_id", 1) or "") != turn_id
                    or str(_field(run_row, "topic_id", 2) or "") != topic_id
                    or stored_accepted_attempt_ref != accepted_attempt_ref
                    or str(entry.get("transition_attempt_id") or "")
                    != expected_transition.attempt_id
                    or str(entry.get("accepted_output_digest") or "")
                    != accepted_call.acceptance.output_digest
                ):
                    raise EvidenceIntegrityError("conversation_entry_binding_conflict")
                self._conversation_entry_failpoint("before_commit")
                self.connection.commit()
                return "replayed"
            if stored_accepted_attempt_ref:
                raise EvidenceIntegrityError("conversation_entry_binding_conflict")

            if topic is not None:
                existing_topic = self.connection.execute(
                    """
                    SELECT thread_id, title, summary, status,
                           assumptions, open_questions
                    FROM waje_runtime.conversation_topics
                    WHERE topic_id = %(topic_id)s
                    FOR UPDATE
                    """,
                    {"topic_id": topic.topic_id},
                ).fetchone()
                if topic_is_new:
                    if existing_topic is None:
                        self.connection.execute(
                            """
                            INSERT INTO waje_runtime.conversation_topics(
                              topic_id, thread_id, title, summary, status,
                              assumptions, open_questions
                            ) VALUES (
                              %(topic_id)s, %(thread_id)s, %(title)s,
                              %(summary)s, %(status)s, %(assumptions)s::jsonb,
                              %(open_questions)s::jsonb
                            )
                            """,
                            {
                                **topic.to_dict(),
                                "assumptions": _json(topic.assumptions),
                                "open_questions": _json(topic.open_questions),
                            },
                        )
                    elif not _topic_row_matches(existing_topic, topic):
                        raise EvidenceIntegrityError(
                            "conversation_entry_topic_conflict"
                        )
                elif existing_topic is None or not _topic_row_matches(
                    existing_topic, topic
                ):
                    raise EvidenceIntegrityError("conversation_entry_topic_invalid")
                if set_current_topic:
                    self.connection.execute(
                        """
                        UPDATE waje_runtime.investigation_threads
                        SET current_topic_id = %(topic_id)s, updated_at = now()
                        WHERE thread_id = %(thread_id)s
                        """,
                        {"thread_id": thread_id, "topic_id": topic.topic_id},
                    )
            elif set_current_topic:
                raise EvidenceIntegrityError("conversation_entry_topic_invalid")
            self._conversation_entry_failpoint("after_topic")
            self.connection.execute(
                """
                INSERT INTO waje_runtime.conversation_turns(
                  turn_id, thread_id, topic_id, intent, payload
                ) VALUES (
                  %(turn_id)s, %(thread_id)s, %(topic_id)s,
                  %(intent)s, %(payload)s::jsonb
                )
                """,
                {
                    "turn_id": turn_id,
                    "thread_id": thread_id,
                    "topic_id": topic_id or None,
                    "intent": str(
                        turn_payload.get("turn_intent", {}).get("intent") or ""
                    ),
                    "payload": _json(turn_payload),
                },
            )
            self._conversation_entry_failpoint("after_turn")
            self.connection.execute(
                """
                INSERT INTO waje_runtime.context_manifests(
                  manifest_id, thread_id, turn_id, topic_id, run_id,
                  can_support_claims, items, manifest_digest, payload
                ) VALUES (
                  %(manifest_id)s, %(thread_id)s, %(turn_id)s,
                  %(topic_id)s, %(run_id)s, %(can_support_claims)s,
                  %(items)s::jsonb, '', '{}'::jsonb
                )
                """,
                {
                    "manifest_id": manifest_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "topic_id": topic_id or None,
                    "run_id": run_attempt_id,
                    "can_support_claims": bool(
                        manifest_payload.get("can_support_claims")
                    ),
                    "items": _json(manifest_payload),
                },
            )
            self._conversation_entry_failpoint("after_manifest")
            existing_turn_id = str(_field(run_row, "turn_id", 1) or "")
            existing_topic_id = str(_field(run_row, "topic_id", 2) or "")
            if existing_turn_id not in {"", turn_id} or existing_topic_id not in {
                "",
                topic_id,
            }:
                raise EvidenceIntegrityError("conversation_entry_run_binding_conflict")
            updated_request = canonical_value(
                {
                    **dict(request),
                    "conversation_entry": {
                        **dict(state),
                        "accepted_attempt_ref": accepted_attempt_ref,
                        "accepted_output_digest": (
                            accepted_call.acceptance.output_digest
                        ),
                        "transition_attempt_id": expected_transition.attempt_id,
                    },
                }
            )
            cursor = self.connection.execute(
                """
                UPDATE waje_runtime.analysis_runs
                SET turn_id = %(turn_id)s,
                    topic_id = %(topic_id)s,
                    request = %(request)s::jsonb,
                    updated_at = now()
                WHERE run_id = %(run_attempt_id)s
                  AND run_attempt_id = %(run_attempt_id)s
                  AND thread_id = %(thread_id)s
                  AND (turn_id IS NULL OR turn_id = %(turn_id)s)
                  AND (topic_id IS NULL OR topic_id IS NOT DISTINCT FROM %(topic_id)s)
                """,
                {
                    "run_attempt_id": run_attempt_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "topic_id": topic_id or None,
                    "request": _json(updated_request),
                },
            )
            if getattr(cursor, "rowcount", 1) != 1:
                raise EvidenceIntegrityError("conversation_entry_run_binding_conflict")
            self._conversation_entry_failpoint("before_commit")
            self.connection.commit()
            return "accepted"
        except Exception:
            self.connection.rollback()
            raise

    def _conversation_entry_rows_match(
        self,
        *,
        existing_turn: Any,
        existing_manifest: Any,
        thread_id: str,
        topic_id: str,
        turn_payload: Mapping[str, Any],
        manifest_payload: Mapping[str, Any],
    ) -> bool:
        from bi_agent.runtime.evidence_authority import canonical_value

        if existing_turn is None or existing_manifest is None:
            return False
        stored_turn = _json_value(_field(existing_turn, "payload", 3))
        stored_manifest = _json_value(_field(existing_manifest, "items", 5))
        return (
            str(_field(existing_turn, "thread_id", 0) or "") == thread_id
            and str(_field(existing_turn, "topic_id", 1) or "") == topic_id
            and canonical_value(stored_turn) == canonical_value(turn_payload)
            and str(_field(existing_manifest, "thread_id", 0) or "") == thread_id
            and str(_field(existing_manifest, "turn_id", 1) or "")
            == str(turn_payload.get("turn_id") or "")
            and str(_field(existing_manifest, "topic_id", 2) or "") == topic_id
            and str(_field(existing_manifest, "run_id", 3) or "")
            == str(manifest_payload.get("run_id") or "")
            and canonical_value(stored_manifest) == canonical_value(manifest_payload)
        )

    def _conversation_entry_failpoint(self, stage: str) -> None:
        del stage

    def active_conversation_run_status(
        self,
        thread_id: str,
        *,
        exclude_run_id: str,
    ) -> str:
        row = self._fetchone(
            """
            SELECT 1
            FROM waje_runtime.analysis_runs
            WHERE thread_id = %(thread_id)s
              AND run_id <> %(exclude_run_id)s
              AND status IN ('queued', 'running')
            LIMIT 1
            """,
            {"thread_id": thread_id, "exclude_run_id": exclude_run_id},
        )
        return "running" if row is not None else "idle"

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

    def set_pending_clarification(
        self, thread_id: str, topic_id: str, clarification_id: str
    ) -> None:
        self._execute(
            """
            UPDATE waje_runtime.investigation_threads
            SET pending_clarification_topic_id = %(topic_id)s,
                pending_clarification_id = %(clarification_id)s,
                updated_at = now()
            WHERE thread_id = %(thread_id)s
            """,
            {
                "thread_id": thread_id,
                "topic_id": topic_id,
                "clarification_id": clarification_id,
            },
        )
        self._audit(
            "clarification_pending",
            thread_id=thread_id,
            topic_id=topic_id,
            ref=clarification_id,
        )

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
                {
                    "thread_id": thread_id,
                    "topic_id": state.topic_id,
                    "run_id": state.run_id,
                },
            )
        self._audit(
            "clarification_state_saved",
            thread_id=thread_id,
            topic_id=state.topic_id,
            run_id=state.run_id,
            ref=state.run_id,
            payload=state.to_dict(),
        )

    def finalize_waiting_clarification(
        self,
        *,
        run_id: str,
        thread_id: str,
        turn_id: str,
        topic_id: str,
        request: Mapping[str, Any],
        clarification_state: ClarificationState,
    ) -> str:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        if (
            clarification_state.run_id != run_id
            or clarification_state.topic_id != topic_id
            or clarification_state.status != "waiting"
        ):
            raise EvidenceIntegrityError("waiting_clarification_state_owner_mismatch")
        finalized_request = canonical_value(dict(request))
        params = {
            "run_id": run_id,
            "thread_id": thread_id,
            "turn_id": turn_id or None,
            "topic_id": topic_id,
            "request": _json(finalized_request),
        }
        active_dispatch = self._active_run_dispatches.get(run_id)
        action = "transition"
        try:
            if active_dispatch is not None:
                self._lock_active_run_dispatch(
                    dispatch_id=active_dispatch[0],
                    run_id=run_id,
                    dispatch_owner_id=active_dispatch[1],
                    lease_epoch=active_dispatch[2],
                )
            current = self._fetchone(
                """
                /* waiting_clarification_run_lock */
                SELECT status, thread_id, turn_id, topic_id, request
                FROM waje_runtime.analysis_runs
                WHERE run_id = %(run_id)s
                FOR UPDATE
                """,
                params,
            )
            thread = self._fetchone(
                """
                /* waiting_clarification_thread_lock */
                SELECT pending_clarification_topic_id,
                       pending_clarification_id
                FROM waje_runtime.investigation_threads
                WHERE thread_id = %(thread_id)s
                FOR UPDATE
                """,
                params,
            )
            topic = self._fetchone(
                """
                SELECT thread_id
                FROM waje_runtime.conversation_topics
                WHERE topic_id = %(topic_id)s
                """,
                params,
            )
            if (
                current is None
                or thread is None
                or topic is None
                or str(_field(topic, "thread_id", 0) or "") != thread_id
            ):
                raise EvidenceIntegrityError("waiting_clarification_source_missing")
            action = validate_run_status_transition(
                current_status=str(_field(current, "status", 0) or ""),
                next_status="waiting_for_clarification",
                current_thread_id=str(_field(current, "thread_id", 1) or ""),
                current_turn_id=str(_field(current, "turn_id", 2) or ""),
                current_topic_id=str(_field(current, "topic_id", 3) or ""),
                next_thread_id=thread_id,
                next_turn_id=turn_id,
                next_topic_id=topic_id,
                current_request=_json_value(_field(current, "request", 4)) or {},
                next_request=finalized_request,
            )
            if action == "replay":
                state_row = self._fetchone(
                    """
                    SELECT payload
                    FROM waje_runtime.audit_events
                    WHERE run_id = %(run_id)s
                      AND event_type = 'clarification_state_saved'
                    ORDER BY created_at DESC, audit_id DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    params,
                )
                if (
                    str(
                        _field(
                            thread,
                            "pending_clarification_topic_id",
                            0,
                        )
                        or ""
                    )
                    != topic_id
                    or str(_field(thread, "pending_clarification_id", 1) or "")
                    != run_id
                    or state_row is None
                    or canonical_value(
                        _json_value(_field(state_row, "payload", 0)) or {}
                    )
                    != canonical_value(clarification_state.to_dict())
                ):
                    raise EvidenceIntegrityError(
                        "waiting_clarification_replay_conflict"
                    )
            else:
                updated = self._execute(
                    """
                    /* waiting_clarification_run_transition */
                    UPDATE waje_runtime.analysis_runs
                    SET status = 'waiting_for_clarification',
                        request = %(request)s::jsonb,
                        turn_id = %(turn_id)s,
                        topic_id = %(topic_id)s,
                        updated_at = now()
                    WHERE run_id = %(run_id)s
                      AND status = %(current_status)s
                    RETURNING status
                    """,
                    {
                        **params,
                        "current_status": str(_field(current, "status", 0) or ""),
                    },
                    commit=False,
                ).fetchone()
                if updated is None:
                    raise EvidenceIntegrityError(
                        "analysis_run_status_transition_conflict"
                    )
                pending = self._execute(
                    """
                    /* waiting_clarification_pending_transition */
                    UPDATE waje_runtime.investigation_threads
                    SET pending_clarification_topic_id = %(topic_id)s,
                        pending_clarification_id = %(run_id)s,
                        updated_at = now()
                    WHERE thread_id = %(thread_id)s
                    RETURNING pending_clarification_id
                    """,
                    params,
                    commit=False,
                ).fetchone()
                if pending is None:
                    raise EvidenceIntegrityError(
                        "waiting_clarification_thread_update_failed"
                    )
                self._audit(
                    "run_status_changed",
                    thread_id=thread_id,
                    topic_id=topic_id,
                    run_id=run_id,
                    ref=run_id,
                    payload={"status": "waiting_for_clarification"},
                    commit=False,
                )
                self._audit(
                    "clarification_pending",
                    thread_id=thread_id,
                    topic_id=topic_id,
                    run_id=run_id,
                    ref=run_id,
                    commit=False,
                )
                self._audit(
                    "clarification_state_saved",
                    thread_id=thread_id,
                    topic_id=topic_id,
                    run_id=run_id,
                    ref=run_id,
                    payload=clarification_state.to_dict(),
                    commit=False,
                )
            if active_dispatch is not None:
                self._terminalize_active_run_dispatch(
                    dispatch_id=active_dispatch[0],
                    run_id=run_id,
                    dispatch_owner_id=active_dispatch[1],
                    lease_epoch=active_dispatch[2],
                    status="waiting_for_clarification",
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        if active_dispatch is not None:
            self._stop_run_dispatch_heartbeat(active_dispatch[0])
            self._active_run_dispatches.pop(run_id, None)
        return "replayed" if action == "replay" else "inserted"

    def get_open_clarification(self, thread_id: str) -> Optional[ClarificationState]:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
            canonical_value,
        )

        thread = self.get_thread(thread_id)
        pending_run_id = thread.pending_clarification_id
        pending_topic_id = thread.pending_clarification_topic_id
        if not pending_run_id and not pending_topic_id:
            return None
        if not pending_run_id or not pending_topic_id:
            raise EvidenceIntegrityError("pending_clarification_owner_closure_invalid")
        run_state = self.get_run_state(pending_run_id)
        intent = self.resolve_active_intent_revision(pending_run_id)
        if (
            run_state is None
            or run_state.get("thread_id") != thread_id
            or run_state.get("topic_id") != pending_topic_id
            or run_state.get("status") != "waiting_for_clarification"
            or intent is None
        ):
            raise EvidenceIntegrityError("pending_clarification_authority_missing")
        transition_rows = self._fetchall(
            """
            SELECT input_digest
            FROM waje_runtime.workflow_transition_attempts
            WHERE run_attempt_id = %(run_attempt_id)s
              AND intent_revision_id = %(intent_revision_id)s
              AND node_name = 'generate_clarification'
              AND status = 'succeeded'
              AND acceptance_state = 'accepted'
            ORDER BY created_at DESC, attempt_id DESC
            LIMIT 2
            """,
            {
                "run_attempt_id": pending_run_id,
                "intent_revision_id": intent.intent_revision_id,
            },
        )
        if len(transition_rows) != 1:
            raise EvidenceIntegrityError("pending_clarification_transition_invalid")
        accepted = self.load_accepted_transition(
            run_attempt_id=pending_run_id,
            node_name="generate_clarification",
            input_digest=str(_field(transition_rows[0], "input_digest", 0) or ""),
        )
        if accepted is None:
            raise EvidenceIntegrityError("pending_clarification_transition_invalid")
        transition = accepted["transition"]
        transition_input = accepted.get("input_payload") or {}
        ambiguity_slot = transition_input.get("ambiguity_slot")
        active_slot = next(
            (
                slot
                for slot in intent.ambiguity_slots
                if isinstance(ambiguity_slot, Mapping)
                and slot.get("slot_id") == ambiguity_slot.get("slot_id")
            ),
            None,
        )
        if (
            transition.status != "succeeded"
            or transition.intent_revision_id != intent.intent_revision_id
            or transition.next_transition != "persist_waiting_for_decision"
            or transition_input.get("intent_revision_ref") != intent.intent_revision_id
            or active_slot is None
            or canonical_value(ambiguity_slot) != canonical_value(active_slot)
            or len(
                self.attempt_journal.load_stage_attempt_refs(
                    run_attempt_id=pending_run_id,
                    transition_attempt_id=transition.attempt_id,
                    stage_name="generate_clarification",
                )
            )
            != 1
        ):
            raise EvidenceIntegrityError("pending_clarification_transition_invalid")
        output = accepted.get("output_payload") or {}
        transition_options = output.get("decision_options")
        outcome = output.get("clarification_outcome")
        persisted_options = self.load_decision_options(intent.intent_revision_id)
        if (
            not isinstance(transition_options, list)
            or not transition_options
            or not isinstance(outcome, Mapping)
            or canonical_value(transition_options) != canonical_value(persisted_options)
        ):
            raise EvidenceIntegrityError(
                "pending_clarification_decision_options_invalid"
            )
        slot_id = str(active_slot["slot_id"])
        slot_kind = str(active_slot["slot_kind"])
        allowed_value_refs = set(active_slot.get("allowed_value_refs") or ())
        typed_value_key = "baseline_id" if slot_kind == "baseline" else "value_ref"
        for option in persisted_options:
            typed_value = option["typed_value"]
            if (
                option["slot_id"] != slot_id
                or set(typed_value) != {typed_value_key}
                or typed_value[typed_value_key] not in allowed_value_refs
                or option["option_id"] != f"{slot_id}.{typed_value[typed_value_key]}"
            ):
                raise EvidenceIntegrityError(
                    "pending_clarification_decision_options_invalid"
                )
        outcome_options = outcome.get("options")
        question = outcome.get("question")
        if (
            set(outcome)
            != {
                "status",
                "boundary_status",
                "slot_id",
                "slot_kind",
                "question",
                "questions",
                "options",
                "recommendation_reason",
                "status_message",
            }
            or not isinstance(question, str)
            or not question.strip()
            or not isinstance(outcome_options, list)
            or len(outcome_options) != len(persisted_options) + 1
        ):
            raise EvidenceIntegrityError("pending_clarification_projection_invalid")
        expected_projection = [
            {
                "option_id": option["option_id"],
                "label": option["display_label"],
                "description": option["display_description"],
                "recommended": option["recommended"],
                "typed_value": option["typed_value"],
            }
            for option in persisted_options
        ]
        escape_option = outcome_options[-1]
        expected_question_projection = [
            {
                "question": question,
                "options": [option["display_label"] for option in persisted_options]
                + [CLARIFICATION_ESCAPE_OPTION],
            }
        ]
        if (
            canonical_value(outcome_options[:-1])
            != canonical_value(expected_projection)
            or not isinstance(escape_option, Mapping)
            or escape_option.get("option_id") != "tell_agent_differently"
            or escape_option.get("label") != CLARIFICATION_ESCAPE_OPTION
            or not isinstance(escape_option.get("description"), str)
            or not escape_option["description"].strip()
            or escape_option.get("recommended") is not False
            or outcome.get("status") != "question_tool_opened"
            or outcome.get("boundary_status") != "needs_question"
            or outcome.get("slot_id") != slot_id
            or outcome.get("slot_kind") != slot_kind
            or canonical_value(outcome.get("questions"))
            != canonical_value(expected_question_projection)
            or not isinstance(outcome.get("recommendation_reason"), str)
            or not outcome["recommendation_reason"].strip()
            or not isinstance(outcome.get("status_message"), str)
            or not outcome["status_message"].strip()
        ):
            raise EvidenceIntegrityError("pending_clarification_projection_invalid")
        waiting_request = run_state["request"]
        waiting_request_fields = {
            "schema_version",
            "run_attempt_id",
            "thread_id",
            "turn_id",
            "topic_id",
            "turn_intent",
            "topic_relation",
            "intent_revision_id",
            "decision_ledger_position",
            "accepted_transition_id",
            "clarification",
            "context_manifest_ref",
            "runtime_descriptors",
        }
        waiting_transition_id = waiting_request.get("accepted_transition_id")
        if (
            set(waiting_request) != waiting_request_fields
            or waiting_request.get("schema_version")
            != "single-authority-phase02-waiting.v1"
            or waiting_request.get("run_attempt_id") != pending_run_id
            or waiting_request.get("thread_id") != thread_id
            or waiting_request.get("topic_id") != pending_topic_id
            or waiting_request.get("intent_revision_id") != intent.intent_revision_id
            or waiting_request.get("decision_ledger_position")
            != transition.decision_ledger_position
            or not isinstance(waiting_transition_id, str)
            or not waiting_transition_id
            or canonical_value(waiting_request.get("clarification"))
            != canonical_value(outcome)
            or self.latest_accepted_transition_id(pending_run_id)
            != waiting_transition_id
        ):
            raise EvidenceIntegrityError(
                "pending_clarification_waiting_closure_invalid"
            )
        waiting_row = self._fetchone(
            """
            SELECT input_digest
            FROM waje_runtime.workflow_transition_attempts
            WHERE transition_id = %(transition_id)s
              AND run_attempt_id = %(run_attempt_id)s
              AND node_name = 'persist_waiting_for_decision'
              AND status = 'succeeded'
              AND acceptance_state = 'accepted'
            """,
            {
                "transition_id": waiting_transition_id,
                "run_attempt_id": pending_run_id,
            },
        )
        waiting_accepted = (
            self.load_accepted_transition(
                run_attempt_id=pending_run_id,
                node_name="persist_waiting_for_decision",
                input_digest=str(_field(waiting_row, "input_digest", 0) or ""),
            )
            if waiting_row is not None
            else None
        )
        expected_waiting_input = {
            "intent_revision_id": intent.intent_revision_id,
            "decision_ledger_position": transition.decision_ledger_position,
            "decision_options_digest": canonical_digest(persisted_options),
            "clarification_digest": canonical_digest(outcome),
            "parent_transition_id": transition.transition_id,
        }
        if waiting_accepted is None or canonical_value(
            waiting_accepted.get("input_payload")
        ) != canonical_value(expected_waiting_input):
            raise EvidenceIntegrityError(
                "pending_clarification_waiting_closure_invalid"
            )
        waiting_transition = waiting_accepted["transition"]
        waiting_output = waiting_accepted.get("output_payload") or {}
        if set(waiting_output) != {"status", "lifecycle_state"}:
            raise EvidenceIntegrityError(
                "pending_clarification_waiting_closure_invalid"
            )
        waiting_lifecycle = LifecycleState.from_dict(waiting_output["lifecycle_state"])
        latest_lifecycle = self.latest_lifecycle_state(pending_run_id)
        if (
            waiting_transition.transition_id != waiting_transition_id
            or waiting_transition.parent_transition_id != transition.transition_id
            or waiting_transition.intent_revision_id != intent.intent_revision_id
            or waiting_transition.decision_ledger_position
            != transition.decision_ledger_position
            or waiting_transition.next_transition != "await_user_decision"
            or waiting_output["status"] != "waiting_for_clarification"
            or waiting_lifecycle.run_attempt_id != pending_run_id
            or waiting_lifecycle.execution_state != "waiting"
            or waiting_lifecycle.interaction_state != "waiting_for_user"
            or latest_lifecycle != waiting_lifecycle
        ):
            raise EvidenceIntegrityError(
                "pending_clarification_waiting_closure_invalid"
            )
        return ClarificationState(
            run_id=pending_run_id,
            topic_id=pending_topic_id,
            question=question,
            options=[
                ClarificationOption(
                    option_id=str(option["option_id"]),
                    label=str(option["label"]),
                    description=str(option["description"]),
                    recommended=bool(option["recommended"]),
                )
                for option in outcome_options
            ],
        )

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
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        validate_run_status_value(status)
        active_dispatch = self._active_run_dispatches.get(run_id)
        if active_dispatch is not None:
            self._upsert_owned_run(
                run_id,
                dispatch_id=active_dispatch[0],
                thread_id=thread_id,
                turn_id=turn_id,
                topic_id=topic_id,
                status=status,
                request=request or {},
                dispatch_owner_id=active_dispatch[1],
                lease_epoch=active_dispatch[2],
            )
            return
        params = {
            "run_id": run_id,
            "thread_id": thread_id,
            "turn_id": turn_id or None,
            "topic_id": topic_id or None,
            "status": status,
            "request": _json(request or {}),
        }
        try:
            inserted = self._execute(
                """
                /* analysis_run_status_insert */
                INSERT INTO waje_runtime.analysis_runs(
                  run_id, run_attempt_id, thread_id, turn_id, topic_id, status, request
                )
                VALUES (
                  %(run_id)s, %(run_id)s, %(thread_id)s, %(turn_id)s, %(topic_id)s,
                  %(status)s, %(request)s::jsonb
                )
                ON CONFLICT (run_id) DO NOTHING
                RETURNING status
                """,
                params,
                commit=False,
            ).fetchone()
            if inserted is not None:
                self._append_lifecycle_state_locked(
                    LifecycleState.create(
                        run_attempt_id=run_id,
                        execution_state="running",
                    )
                )
                self._audit(
                    "run_status_changed",
                    thread_id=thread_id,
                    topic_id=topic_id,
                    run_id=run_id,
                    ref=run_id,
                    commit=False,
                )
                self.connection.commit()
                return

            current = self._fetchone(
                """
                /* analysis_run_status_transition_lock */
                SELECT status, thread_id, turn_id, topic_id, request
                FROM waje_runtime.analysis_runs
                WHERE run_id = %(run_id)s
                FOR UPDATE
                """,
                {"run_id": run_id},
            )
            if current is None:
                raise EvidenceIntegrityError("analysis_run_status_transition_conflict")
            current_request = _json_value(_field(current, "request", 4))
            if (
                status == "running"
                and self._latest_lifecycle_state_locked(run_id) is None
            ):
                self._append_lifecycle_state_locked(
                    LifecycleState.create(
                        run_attempt_id=run_id,
                        execution_state="running",
                    )
                )
            action = validate_run_status_transition(
                current_status=str(_field(current, "status", 0) or ""),
                next_status=status,
                current_thread_id=str(_field(current, "thread_id", 1) or ""),
                current_turn_id=str(_field(current, "turn_id", 2) or ""),
                current_topic_id=str(_field(current, "topic_id", 3) or ""),
                next_thread_id=thread_id,
                next_turn_id=turn_id,
                next_topic_id=topic_id,
                current_request=current_request,
                next_request=request or {},
            )
            if action == "replay":
                self.connection.commit()
                return

            updated = self._execute(
                """
                /* analysis_run_status_transition_cas */
                UPDATE waje_runtime.analysis_runs
                SET status = %(status)s,
                    request = %(request)s::jsonb,
                    turn_id = %(turn_id)s,
                    topic_id = %(topic_id)s,
                    updated_at = now()
                WHERE run_id = %(run_id)s
                  AND status = %(current_status)s
                RETURNING status
                """,
                {
                    **params,
                    "current_status": str(_field(current, "status", 0) or ""),
                },
                commit=False,
            ).fetchone()
            if updated is None:
                raise EvidenceIntegrityError("analysis_run_status_transition_conflict")
            self._audit(
                "run_status_changed",
                thread_id=thread_id,
                topic_id=topic_id,
                run_id=run_id,
                ref=run_id,
                commit=False,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def claim_run_dispatch(
        self,
        *,
        dispatch_id: str,
        run_id: str,
        thread_id: str,
        dispatch_owner_id: str,
        lease_epoch: int,
    ) -> dict[str, Any]:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        if (
            not all(
                isinstance(value, str) and value.strip()
                for value in (
                    dispatch_id,
                    run_id,
                    thread_id,
                    dispatch_owner_id,
                )
            )
            or not isinstance(lease_epoch, int)
            or isinstance(lease_epoch, bool)
            or lease_epoch <= 0
        ):
            raise EvidenceIntegrityError("run_dispatch_claim_invalid")
        try:
            self._execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
                {"lock_key": f"single_authority:{run_id}"},
                commit=False,
            )
            dispatch = self._fetchone(
                """
                /* generic_run_dispatch_owner_lock */
                SELECT dispatch_id, run_id, thread_id, producer_kind,
                       scope_ref, dispatch_state, owner_id, lease_epoch,
                       lease_expires_at > now() AS lease_active
                FROM waje_runtime.run_dispatches
                WHERE dispatch_id = %(dispatch_id)s
                  AND run_id = %(run_id)s
                FOR UPDATE
                """,
                {"dispatch_id": dispatch_id, "run_id": run_id},
            )
            if dispatch is None:
                raise EvidenceIntegrityError("run_dispatch_claim_missing")
            resolved_dispatch = {
                "dispatch_id": str(_field(dispatch, "dispatch_id", 0) or ""),
                "run_id": str(_field(dispatch, "run_id", 1) or ""),
                "thread_id": str(_field(dispatch, "thread_id", 2) or ""),
                "producer_kind": str(_field(dispatch, "producer_kind", 3) or ""),
                "scope_ref": str(_field(dispatch, "scope_ref", 4) or ""),
                "dispatch_state": str(_field(dispatch, "dispatch_state", 5) or ""),
                "owner_id": str(_field(dispatch, "owner_id", 6) or ""),
                "lease_epoch": int(_field(dispatch, "lease_epoch", 7) or 0),
                "lease_active": bool(_field(dispatch, "lease_active", 8)),
            }
            run = self._fetchone(
                """
                /* generic_run_dispatch_run_lock */
                SELECT run_id, thread_id, turn_id, topic_id, status, request
                FROM waje_runtime.analysis_runs
                WHERE run_id = %(run_id)s
                FOR UPDATE
                """,
                {"run_id": run_id},
            )
            if run is None:
                raise EvidenceIntegrityError("run_dispatch_claim_missing")
            run_status = str(_field(run, "status", 4) or "")
            producer_kind = resolved_dispatch["producer_kind"]
            if (
                resolved_dispatch["dispatch_id"] != dispatch_id
                or resolved_dispatch["run_id"] != run_id
                or resolved_dispatch["thread_id"] != thread_id
                or resolved_dispatch["dispatch_state"] != "leased"
                or resolved_dispatch["owner_id"] != dispatch_owner_id
                or resolved_dispatch["lease_epoch"] != lease_epoch
                or resolved_dispatch["lease_active"] is not True
                or str(_field(run, "run_id", 0) or "") != run_id
                or str(_field(run, "thread_id", 1) or "") != thread_id
                or producer_kind not in {"thread_message", "clarification_resolution"}
                or (
                    producer_kind == "thread_message"
                    and (
                        resolved_dispatch["scope_ref"] != thread_id
                        or run_status != "queued"
                    )
                )
                or (
                    producer_kind == "clarification_resolution"
                    and (
                        resolved_dispatch["scope_ref"] != run_id
                        or run_status != "waiting_for_clarification"
                    )
                )
            ):
                raise EvidenceIntegrityError("run_dispatch_claim_rejected")
            lifecycle = self._latest_lifecycle_state_locked(run_id)
            if producer_kind == "thread_message":
                if lifecycle is not None:
                    raise EvidenceIntegrityError("run_dispatch_lifecycle_conflict")
                updated_run = self._execute(
                    """
                    /* generic_run_dispatch_run_claim_cas */
                    UPDATE waje_runtime.analysis_runs
                    SET status = 'running', updated_at = now()
                    WHERE run_id = %(run_id)s
                      AND thread_id = %(thread_id)s
                      AND status = 'queued'
                    RETURNING status
                    """,
                    {"run_id": run_id, "thread_id": thread_id},
                    commit=False,
                ).fetchone()
            else:
                if (
                    lifecycle is None
                    or lifecycle.execution_state != "waiting"
                    or lifecycle.interaction_state != "waiting_for_user"
                ):
                    raise EvidenceIntegrityError("run_dispatch_lifecycle_conflict")
                updated_run = run
            updated_dispatch = self._execute(
                """
                /* generic_run_dispatch_owner_consume_cas */
                UPDATE waje_runtime.run_dispatches
                SET dispatch_state = 'running',
                    lease_expires_at = now()
                      + (%(lease_ms)s * interval '1 millisecond'),
                    heartbeat_at = now(), updated_at = now()
                WHERE dispatch_id = %(dispatch_id)s
                  AND run_id = %(run_id)s
                  AND dispatch_state = 'leased'
                  AND owner_id = %(owner_id)s
                  AND lease_epoch = %(lease_epoch)s
                  AND lease_expires_at > now()
                RETURNING dispatch_state
                """,
                {
                    "dispatch_id": dispatch_id,
                    "run_id": run_id,
                    "owner_id": dispatch_owner_id,
                    "lease_epoch": lease_epoch,
                    "lease_ms": _run_dispatch_lease_ms(),
                },
                commit=False,
            ).fetchone()
            if updated_run is None or updated_dispatch is None:
                raise EvidenceIntegrityError("run_dispatch_claim_rejected")
            if producer_kind == "thread_message":
                self._append_lifecycle_state_locked(
                    LifecycleState.create(
                        run_attempt_id=run_id,
                        execution_state="running",
                    )
                )
                self._audit(
                    "run_status_changed",
                    thread_id=thread_id,
                    run_id=run_id,
                    ref=run_id,
                    payload={
                        "status": "running",
                        "dispatch_id": dispatch_id,
                        "dispatch_owner_id": dispatch_owner_id,
                        "lease_epoch": lease_epoch,
                    },
                    commit=False,
                )
            self._audit(
                "run_dispatch_claimed",
                thread_id=thread_id,
                run_id=run_id,
                ref=dispatch_id,
                payload={
                    "dispatch_id": dispatch_id,
                    "producer_kind": producer_kind,
                    "dispatch_owner_id": dispatch_owner_id,
                    "lease_epoch": lease_epoch,
                },
                commit=False,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        self._active_run_dispatches[run_id] = (
            dispatch_id,
            dispatch_owner_id,
            lease_epoch,
        )
        self._start_run_dispatch_heartbeat(
            dispatch_id=dispatch_id,
            run_id=run_id,
            dispatch_owner_id=dispatch_owner_id,
            lease_epoch=lease_epoch,
        )
        return canonical_value(
            {
                "run_id": run_id,
                "thread_id": thread_id,
                "dispatch_id": dispatch_id,
                "producer_kind": producer_kind,
                "dispatch_owner_id": dispatch_owner_id,
                "lease_epoch": lease_epoch,
                "dispatch_state": "running",
                "status": "running"
                if producer_kind == "thread_message"
                else run_status,
            }
        )

    def renew_run_dispatch_lease(
        self,
        *,
        dispatch_id: str,
        run_id: str,
        dispatch_owner_id: str,
        lease_epoch: int,
    ) -> bool:
        try:
            renewed = self._execute(
                """
                /* generic_run_dispatch_heartbeat_cas */
                UPDATE waje_runtime.run_dispatches dispatch
                SET lease_expires_at = now()
                      + (%(lease_ms)s * interval '1 millisecond'),
                    heartbeat_at = now(), updated_at = now()
                FROM waje_runtime.analysis_runs run
                WHERE dispatch.dispatch_id = %(dispatch_id)s
                  AND dispatch.run_id = %(run_id)s
                  AND run.run_id = dispatch.run_id
                  AND dispatch.dispatch_state = 'running'
                  AND dispatch.owner_id = %(owner_id)s
                  AND dispatch.lease_epoch = %(lease_epoch)s
                  AND dispatch.lease_expires_at > now()
                  AND run.status IN (
                    'running', 'running_workflow', 'waiting_for_clarification'
                  )
                RETURNING dispatch.run_id
                """,
                {
                    "dispatch_id": dispatch_id,
                    "run_id": run_id,
                    "owner_id": dispatch_owner_id,
                    "lease_epoch": lease_epoch,
                    "lease_ms": _run_dispatch_lease_ms(),
                },
                commit=False,
            ).fetchone()
            self.connection.commit()
            return renewed is not None
        except Exception:
            self.connection.rollback()
            raise

    def sweep_expired_run_dispatches(
        self,
        *,
        limit: int = 100,
        thread_id: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        from bi_agent.runtime.evidence_authority import canonical_value

        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("run_dispatch_sweep_limit_invalid")
        if thread_id is not None and (
            not isinstance(thread_id, str)
            or not thread_id
            or thread_id != thread_id.strip()
        ):
            raise ValueError("run_dispatch_sweep_thread_invalid")
        recovered: list[dict[str, Any]] = []
        try:
            rows = self._fetchall(
                """
                /* expired_run_dispatch_scan */
                SELECT dispatch.dispatch_id, dispatch.run_id,
                       dispatch.thread_id, dispatch.producer_kind,
                       dispatch.dispatch_state, dispatch.owner_id,
                       dispatch.lease_epoch, false AS lease_active,
                       run.status AS run_status
                FROM waje_runtime.run_dispatches dispatch
                JOIN waje_runtime.analysis_runs run
                  ON run.run_id = dispatch.run_id
                WHERE dispatch.dispatch_state IN ('leased', 'running')
                  AND dispatch.lease_expires_at <= now()
                  AND (
                    %(thread_id)s::text IS NULL
                    OR dispatch.thread_id = %(thread_id)s
                  )
                ORDER BY dispatch.lease_expires_at, dispatch.dispatch_id
                LIMIT %(limit)s
                FOR UPDATE OF dispatch, run SKIP LOCKED
                """,
                {"limit": limit, "thread_id": thread_id},
            )
            for row in rows:
                dispatch_id = str(_field(row, "dispatch_id", 0) or "")
                run_id = str(_field(row, "run_id", 1) or "")
                thread_id = str(_field(row, "thread_id", 2) or "")
                producer_kind = str(_field(row, "producer_kind", 3) or "")
                state = str(_field(row, "dispatch_state", 4) or "")
                owner_id = str(_field(row, "owner_id", 5) or "")
                lease_epoch = int(_field(row, "lease_epoch", 6) or 0)
                run_status = str(_field(row, "run_status", 8) or "")
                releasable_status = (
                    producer_kind == "thread_message" and run_status == "queued"
                ) or (
                    producer_kind == "clarification_resolution"
                    and run_status == "waiting_for_clarification"
                )
                if state == "leased" and releasable_status:
                    released = self._execute(
                        """
                        /* expired_leased_dispatch_release_cas */
                        UPDATE waje_runtime.run_dispatches
                        SET dispatch_state = 'pending', owner_id = NULL,
                            lease_expires_at = NULL, heartbeat_at = NULL,
                            updated_at = now()
                        WHERE dispatch_id = %(dispatch_id)s
                          AND run_id = %(run_id)s
                          AND dispatch_state = 'leased'
                          AND owner_id = %(owner_id)s
                          AND lease_epoch = %(lease_epoch)s
                        RETURNING dispatch_state
                        """,
                        {
                            "dispatch_id": dispatch_id,
                            "run_id": run_id,
                            "owner_id": owner_id,
                            "lease_epoch": lease_epoch,
                        },
                        commit=False,
                    ).fetchone()
                    if released is not None:
                        recovered.append(
                            {
                                "dispatch_id": dispatch_id,
                                "run_id": run_id,
                                "action": "released_for_retry",
                            }
                        )
                    continue
                if state != "running":
                    continue
                preserve_waiting_run = (
                    producer_kind == "clarification_resolution"
                    and run_status == "waiting_for_clarification"
                )
                if preserve_waiting_run:
                    released = self._execute(
                        """
                        /* expired_running_clarification_release_cas */
                        UPDATE waje_runtime.run_dispatches
                        SET dispatch_state = 'pending', owner_id = NULL,
                            lease_expires_at = NULL, heartbeat_at = NULL,
                            failure_reason = NULL, updated_at = now()
                        WHERE dispatch_id = %(dispatch_id)s
                          AND run_id = %(run_id)s
                          AND dispatch_state = 'running'
                          AND owner_id = %(owner_id)s
                          AND lease_epoch = %(lease_epoch)s
                        RETURNING dispatch_state
                        """,
                        {
                            "dispatch_id": dispatch_id,
                            "run_id": run_id,
                            "owner_id": owner_id,
                            "lease_epoch": lease_epoch,
                        },
                        commit=False,
                    ).fetchone()
                    if released is not None:
                        self._audit(
                            "run_dispatch_recovery_requested",
                            thread_id=thread_id,
                            run_id=run_id,
                            ref=dispatch_id,
                            payload={
                                "dispatch_id": dispatch_id,
                                "producer_kind": producer_kind,
                                "failure_reason": ("run_dispatch_heartbeat_expired"),
                                "lease_epoch": lease_epoch,
                            },
                            commit=False,
                        )
                        recovered.append(
                            {
                                "dispatch_id": dispatch_id,
                                "run_id": run_id,
                                "action": "released_for_retry",
                            }
                        )
                    continue
                if not preserve_waiting_run and run_status not in {
                    "running",
                    "running_workflow",
                }:
                    continue
                failed = self._execute(
                    """
                    /* expired_running_dispatch_run_fail_cas */
                    UPDATE waje_runtime.analysis_runs
                    SET status = 'failed',
                        request = COALESCE(request, '{}'::jsonb)
                          || jsonb_build_object(
                            'failure_reason',
                            'run_dispatch_heartbeat_expired'
                          ),
                        updated_at = now()
                    WHERE run_id = %(run_id)s
                      AND status IN ('running', 'running_workflow')
                    RETURNING status
                    """,
                    {"run_id": run_id},
                    commit=False,
                ).fetchone()
                terminal = self._execute(
                    """
                    /* expired_running_dispatch_terminal_cas */
                    UPDATE waje_runtime.run_dispatches
                    SET dispatch_state = 'terminal',
                        terminal_status = 'failed',
                        failure_reason = 'run_dispatch_heartbeat_expired',
                        lease_expires_at = NULL, updated_at = now()
                    WHERE dispatch_id = %(dispatch_id)s
                      AND run_id = %(run_id)s
                      AND dispatch_state = 'running'
                      AND owner_id = %(owner_id)s
                      AND lease_epoch = %(lease_epoch)s
                    RETURNING dispatch_state
                    """,
                    {
                        "dispatch_id": dispatch_id,
                        "run_id": run_id,
                        "owner_id": owner_id,
                        "lease_epoch": lease_epoch,
                    },
                    commit=False,
                ).fetchone()
                if terminal is None or failed is None:
                    continue
                self._audit(
                    "run_dispatch_failed",
                    thread_id=thread_id,
                    run_id=run_id,
                    ref=run_id,
                    payload={
                        "failure_reason": "run_dispatch_heartbeat_expired",
                        "dispatch_id": dispatch_id,
                        "producer_kind": producer_kind,
                        "lease_epoch": lease_epoch,
                    },
                    commit=False,
                )
                recovered.append(
                    {
                        "dispatch_id": dispatch_id,
                        "run_id": run_id,
                        "action": "terminalized_expired_owner",
                    }
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return tuple(canonical_value(item) for item in recovered)

    def lease_recoverable_run_dispatches(
        self,
        *,
        limit: int = 100,
        thread_id: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("run_dispatch_recovery_limit_invalid")
        if thread_id is not None and (
            not isinstance(thread_id, str)
            or not thread_id
            or thread_id != thread_id.strip()
        ):
            raise ValueError("run_dispatch_recovery_thread_invalid")
        leases: list[dict[str, Any]] = []
        try:
            rows = self._fetchall(
                """
                /* recoverable_run_dispatch_scan */
                SELECT dispatch.dispatch_id, dispatch.producer_kind,
                       dispatch.scope_ref, dispatch.request_identity,
                       dispatch.request_digest, dispatch.request_payload,
                       dispatch.run_id, dispatch.thread_id,
                       dispatch.dispatch_state, dispatch.owner_id,
                       dispatch.lease_epoch,
                       dispatch.lease_expires_at <= now() AS lease_expired,
                       run.status AS run_status
                FROM waje_runtime.run_dispatches dispatch
                JOIN waje_runtime.analysis_runs run
                  ON run.run_id = dispatch.run_id
                WHERE (
                    (
                      dispatch.producer_kind = 'thread_message'
                      AND run.status = 'queued'
                    )
                    OR (
                      dispatch.producer_kind = 'clarification_resolution'
                      AND run.status = 'waiting_for_clarification'
                    )
                  )
                  AND (
                    dispatch.dispatch_state = 'pending'
                    OR (
                      dispatch.dispatch_state = 'leased'
                      AND dispatch.lease_expires_at <= now()
                    )
                  )
                  AND (
                    %(thread_id)s::text IS NULL
                    OR dispatch.thread_id = %(thread_id)s
                  )
                ORDER BY COALESCE(
                           dispatch.lease_expires_at,
                           dispatch.created_at
                         ),
                         dispatch.dispatch_id
                LIMIT %(limit)s
                FOR UPDATE OF dispatch, run SKIP LOCKED
                """,
                {"limit": limit, "thread_id": thread_id},
            )
            for row in rows:
                dispatch_id = str(_field(row, "dispatch_id", 0) or "")
                run_id = str(_field(row, "run_id", 6) or "")
                thread_id = str(_field(row, "thread_id", 7) or "")
                producer_kind = str(_field(row, "producer_kind", 1) or "")
                scope_ref = str(_field(row, "scope_ref", 2) or "")
                request_identity = str(_field(row, "request_identity", 3) or "")
                request_digest = str(_field(row, "request_digest", 4) or "")
                request_payload = _json_value(_field(row, "request_payload", 5))
                current_state = str(_field(row, "dispatch_state", 8) or "")
                current_epoch = int(_field(row, "lease_epoch", 10) or 0)
                lease_expired = bool(_field(row, "lease_expired", 11))
                run_status = str(_field(row, "run_status", 12) or "")
                if (
                    not all(
                        value
                        for value in (
                            dispatch_id,
                            run_id,
                            thread_id,
                            producer_kind,
                            scope_ref,
                            request_identity,
                            request_digest,
                        )
                    )
                    or not isinstance(request_payload, Mapping)
                    or producer_kind
                    not in {"thread_message", "clarification_resolution"}
                    or (
                        producer_kind == "thread_message"
                        and (run_status != "queued" or scope_ref != thread_id)
                    )
                    or (
                        producer_kind == "clarification_resolution"
                        and (
                            run_status != "waiting_for_clarification"
                            or scope_ref != run_id
                        )
                    )
                    or current_state not in {"pending", "leased"}
                    or (current_state == "leased" and not lease_expired)
                ):
                    raise EvidenceIntegrityError("run_dispatch_recovery_record_invalid")
                owner_id = f"recovery-dispatch-{uuid4()}"
                leased = self._execute(
                    """
                    /* recoverable_run_dispatch_lease_cas */
                    UPDATE waje_runtime.run_dispatches dispatch
                    SET dispatch_state = 'leased',
                        owner_id = %(owner_id)s,
                        lease_epoch = lease_epoch + 1,
                        lease_expires_at = now()
                          + (%(lease_ms)s * interval '1 millisecond'),
                        heartbeat_at = now(), updated_at = now()
                    FROM waje_runtime.analysis_runs run
                    WHERE dispatch.dispatch_id = %(dispatch_id)s
                      AND dispatch.run_id = %(run_id)s
                      AND run.run_id = dispatch.run_id
                      AND (
                        (
                          dispatch.producer_kind = 'thread_message'
                          AND run.status = 'queued'
                        )
                        OR (
                          dispatch.producer_kind = 'clarification_resolution'
                          AND run.status = 'waiting_for_clarification'
                        )
                      )
                      AND dispatch.lease_epoch = %(current_epoch)s
                      AND (
                        dispatch.dispatch_state = 'pending'
                        OR (
                          dispatch.dispatch_state = 'leased'
                          AND dispatch.lease_expires_at <= now()
                        )
                      )
                    RETURNING dispatch.lease_epoch
                    """,
                    {
                        "dispatch_id": dispatch_id,
                        "run_id": run_id,
                        "owner_id": owner_id,
                        "current_epoch": current_epoch,
                        "lease_ms": _run_dispatch_lease_ms(),
                    },
                    commit=False,
                ).fetchone()
                if leased is None:
                    raise EvidenceIntegrityError("run_dispatch_recovery_lease_conflict")
                lease_epoch = int(_field(leased, "lease_epoch", 0) or 0)
                self._audit(
                    "run_dispatch_recovery_leased",
                    thread_id=thread_id,
                    run_id=run_id,
                    ref=run_id,
                    payload={
                        "dispatch_id": dispatch_id,
                        "dispatch_owner_id": owner_id,
                        "lease_epoch": lease_epoch,
                        "producer_kind": producer_kind,
                    },
                    commit=False,
                )
                leases.append(
                    canonical_value(
                        {
                            "dispatch_id": dispatch_id,
                            "run_id": run_id,
                            "thread_id": thread_id,
                            "producer_kind": producer_kind,
                            "scope_ref": scope_ref,
                            "request_identity": request_identity,
                            "request_digest": request_digest,
                            "request_payload": dict(request_payload),
                            "dispatch_owner_id": owner_id,
                            "lease_epoch": lease_epoch,
                        }
                    )
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return tuple(leases)

    def fail_owned_run_dispatch(
        self,
        *,
        dispatch_id: str,
        run_id: str,
        thread_id: str,
        dispatch_owner_id: str,
        lease_epoch: int,
        failure_reason: str,
    ) -> dict[str, Any]:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        if (
            not all(
                isinstance(value, str) and value.strip()
                for value in (
                    dispatch_id,
                    run_id,
                    thread_id,
                    dispatch_owner_id,
                    failure_reason,
                )
            )
            or not isinstance(lease_epoch, int)
            or isinstance(lease_epoch, bool)
            or lease_epoch <= 0
        ):
            raise EvidenceIntegrityError("run_dispatch_failure_invalid")
        try:
            dispatch = self._fetchone(
                """
                /* recovery_run_dispatch_owner_lock */
                SELECT dispatch_id, run_id, thread_id, producer_kind,
                       dispatch_state, owner_id, lease_epoch,
                       lease_expires_at > now() AS lease_active,
                       terminal_status, failure_reason
                FROM waje_runtime.run_dispatches
                WHERE dispatch_id = %(dispatch_id)s
                  AND run_id = %(run_id)s
                FOR UPDATE
                """,
                {"dispatch_id": dispatch_id, "run_id": run_id},
            )
            run = self._fetchone(
                """
                /* recovery_run_dispatch_run_lock */
                SELECT run_id, thread_id, turn_id, topic_id, status, request
                FROM waje_runtime.analysis_runs
                WHERE run_id = %(run_id)s
                FOR UPDATE
                """,
                {"run_id": run_id},
            )
            dispatch_state = str(_field(dispatch, "dispatch_state", 4) or "")
            producer_kind = str(_field(dispatch, "producer_kind", 3) or "")
            owner_matches = (
                dispatch is not None
                and str(_field(dispatch, "dispatch_id", 0) or "") == dispatch_id
                and str(_field(dispatch, "run_id", 1) or "") == run_id
                and str(_field(dispatch, "thread_id", 2) or "") == thread_id
                and str(_field(dispatch, "owner_id", 5) or "") == dispatch_owner_id
                and int(_field(dispatch, "lease_epoch", 6) or 0) == lease_epoch
            )
            run_status = str(_field(run, "status", 4) or "")
            terminal_status = str(_field(dispatch, "terminal_status", 8) or "")
            failed_clarification_replay = (
                producer_kind == "clarification_resolution"
                and dispatch_state == "terminal"
                and terminal_status == "failed"
                and run_status == "waiting_for_clarification"
            )
            if (
                dispatch is not None
                and run is not None
                and owner_matches
                and dispatch_state == "terminal"
                and (
                    failed_clarification_replay
                    or (
                        run_status
                        in {
                            "planned",
                            "evidence_ready",
                            "authority_sealed",
                            "narrative_ready",
                            "waiting_for_clarification",
                            "completed",
                            "interaction_completed",
                            "failed",
                        }
                        and terminal_status == run_status
                    )
                )
            ):
                durable_request = _json_value(_field(run, "request", 5))
                durable_failure_reason = str(
                    (
                        durable_request.get("failure_reason")
                        if isinstance(durable_request, Mapping)
                        else ""
                    )
                    or _field(dispatch, "failure_reason", 9)
                    or ""
                )
                self.connection.commit()
                return canonical_value(
                    {
                        "run_id": run_id,
                        "thread_id": thread_id,
                        "status": run_status,
                        "dispatch_id": dispatch_id,
                        "dispatch_status": terminal_status,
                        **(
                            {"failure_reason": durable_failure_reason}
                            if durable_failure_reason
                            else {}
                        ),
                    }
                )
            if (
                dispatch is None
                or run is None
                or str(_field(dispatch, "dispatch_id", 0) or "") != dispatch_id
                or str(_field(dispatch, "run_id", 1) or "") != run_id
                or str(_field(dispatch, "thread_id", 2) or "") != thread_id
                or producer_kind not in {"thread_message", "clarification_resolution"}
                or dispatch_state not in {"leased", "running"}
                or not owner_matches
                or not bool(_field(dispatch, "lease_active", 7))
                or str(_field(run, "run_id", 0) or "") != run_id
                or str(_field(run, "thread_id", 1) or "") != thread_id
                or run_status
                not in {
                    "queued",
                    "running",
                    "running_workflow",
                    "waiting_for_clarification",
                }
            ):
                raise EvidenceIntegrityError("run_dispatch_owner_lost")
            preserve_waiting_run = (
                producer_kind == "clarification_resolution"
                and run_status == "waiting_for_clarification"
            )
            if run_status == "waiting_for_clarification" and not preserve_waiting_run:
                raise EvidenceIntegrityError("run_dispatch_owner_lost")
            request = _json_value(_field(run, "request", 5))
            failed_request = canonical_value(
                {
                    **(dict(request) if isinstance(request, Mapping) else {}),
                    "failure_reason": failure_reason,
                }
            )
            failed = None
            if not preserve_waiting_run:
                failed = self._execute(
                    """
                    /* recovery_run_dispatch_failure_cas */
                    UPDATE waje_runtime.analysis_runs
                    SET status = 'failed', request = %(request)s::jsonb,
                        updated_at = now()
                    WHERE run_id = %(run_id)s
                      AND thread_id = %(thread_id)s
                      AND status = %(current_status)s
                    RETURNING status
                    """,
                    {
                        "run_id": run_id,
                        "thread_id": thread_id,
                        "current_status": run_status,
                        "request": _json(failed_request),
                    },
                    commit=False,
                ).fetchone()
            terminal = self._execute(
                """
                /* recovery_run_dispatch_terminal_cas */
                UPDATE waje_runtime.run_dispatches
                SET dispatch_state = 'terminal',
                    terminal_status = 'failed',
                    failure_reason = %(failure_reason)s,
                    lease_expires_at = NULL,
                    heartbeat_at = now(), updated_at = now()
                WHERE dispatch_id = %(dispatch_id)s
                  AND run_id = %(run_id)s
                  AND dispatch_state IN ('leased', 'running')
                  AND owner_id = %(owner_id)s
                  AND lease_epoch = %(lease_epoch)s
                  AND lease_expires_at > now()
                RETURNING dispatch_state
                """,
                {
                    "dispatch_id": dispatch_id,
                    "run_id": run_id,
                    "owner_id": dispatch_owner_id,
                    "lease_epoch": lease_epoch,
                    "failure_reason": failure_reason,
                },
                commit=False,
            ).fetchone()
            if terminal is None or (not preserve_waiting_run and failed is None):
                raise EvidenceIntegrityError("run_dispatch_owner_lost")
            if not preserve_waiting_run:
                self._audit(
                    "run_status_changed",
                    thread_id=thread_id,
                    run_id=run_id,
                    ref=run_id,
                    payload={"status": "failed"},
                    commit=False,
                )
            self._audit(
                "run_dispatch_failed",
                thread_id=thread_id,
                run_id=run_id,
                ref=dispatch_id,
                payload={
                    "dispatch_id": dispatch_id,
                    "producer_kind": producer_kind,
                    "failure_reason": failure_reason,
                    "dispatch_owner_id": dispatch_owner_id,
                    "lease_epoch": lease_epoch,
                },
                commit=False,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return canonical_value(
            {
                "run_id": run_id,
                "thread_id": thread_id,
                "dispatch_id": dispatch_id,
                "dispatch_status": "failed",
                "status": (
                    "waiting_for_clarification" if preserve_waiting_run else "failed"
                ),
                "failure_reason": failure_reason,
            }
        )

    def _upsert_owned_run(
        self,
        run_id: str,
        *,
        dispatch_id: str,
        thread_id: str,
        turn_id: str,
        topic_id: str,
        status: str,
        request: Mapping[str, Any],
        dispatch_owner_id: str,
        lease_epoch: int,
    ) -> None:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        params = {
            "dispatch_id": dispatch_id,
            "run_id": run_id,
            "thread_id": thread_id,
            "turn_id": turn_id or None,
            "topic_id": topic_id or None,
            "status": status,
            "request": _json(dict(request)),
            "owner_id": dispatch_owner_id,
            "lease_epoch": lease_epoch,
        }
        try:
            dispatch = self._fetchone(
                """
                /* generic_run_dispatch_owner_lock */
                SELECT dispatch_id, run_id, thread_id, dispatch_state, owner_id,
                       lease_epoch, lease_expires_at > now() AS lease_active
                FROM waje_runtime.run_dispatches
                WHERE dispatch_id = %(dispatch_id)s
                  AND run_id = %(run_id)s
                FOR UPDATE
                """,
                params,
            )
            current = self._fetchone(
                """
                /* generic_run_dispatch_run_lock */
                SELECT run_id, thread_id, turn_id, topic_id, status, request
                FROM waje_runtime.analysis_runs
                WHERE run_id = %(run_id)s
                FOR UPDATE
                """,
                params,
            )
            if (
                dispatch is None
                or current is None
                or str(_field(dispatch, "dispatch_id", 0) or "") != dispatch_id
                or str(_field(dispatch, "run_id", 1) or "") != run_id
                or str(_field(dispatch, "dispatch_state", 3) or "") != "running"
                or str(_field(dispatch, "owner_id", 4) or "") != dispatch_owner_id
                or int(_field(dispatch, "lease_epoch", 5) or 0) != lease_epoch
                or not bool(_field(dispatch, "lease_active", 6))
            ):
                raise EvidenceIntegrityError("run_dispatch_owner_lost")
            current_status = str(_field(current, "status", 4) or "")
            action = validate_run_status_transition(
                current_status=current_status,
                next_status=status,
                current_thread_id=str(_field(current, "thread_id", 1) or ""),
                current_turn_id=str(_field(current, "turn_id", 2) or ""),
                current_topic_id=str(_field(current, "topic_id", 3) or ""),
                next_thread_id=thread_id,
                next_turn_id=turn_id,
                next_topic_id=topic_id,
                current_request=_json_value(_field(current, "request", 5)) or {},
                next_request=dict(request),
            )
            if action == "transition":
                updated = self._execute(
                    """
                    /* owned_analysis_run_status_transition_cas */
                    UPDATE waje_runtime.analysis_runs
                    SET status = %(status)s, request = %(request)s::jsonb,
                        turn_id = %(turn_id)s, topic_id = %(topic_id)s,
                        updated_at = now()
                    WHERE run_id = %(run_id)s
                      AND status = %(current_status)s
                    RETURNING status
                    """,
                    {**params, "current_status": current_status},
                    commit=False,
                ).fetchone()
                if updated is None:
                    raise EvidenceIntegrityError(
                        "analysis_run_status_transition_conflict"
                    )
                self._audit(
                    "run_status_changed",
                    thread_id=thread_id,
                    topic_id=topic_id,
                    run_id=run_id,
                    ref=run_id,
                    payload={"status": status},
                    commit=False,
                )
            if status in {
                "planned",
                "evidence_ready",
                "authority_sealed",
                "narrative_ready",
                "waiting_for_clarification",
                "completed",
                "interaction_completed",
                "failed",
            }:
                terminal = self._execute(
                    """
                    /* owned_run_dispatch_terminal_cas */
                    UPDATE waje_runtime.run_dispatches
                    SET dispatch_state = 'terminal',
                        terminal_status = %(status)s,
                        lease_expires_at = NULL,
                        heartbeat_at = now(), updated_at = now()
                    WHERE dispatch_id = %(dispatch_id)s
                      AND run_id = %(run_id)s
                      AND dispatch_state = 'running'
                      AND owner_id = %(owner_id)s
                      AND lease_epoch = %(lease_epoch)s
                    RETURNING dispatch_state
                    """,
                    params,
                    commit=False,
                ).fetchone()
                if terminal is None:
                    raise EvidenceIntegrityError("run_dispatch_owner_lost")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        if status in {
            "planned",
            "evidence_ready",
            "authority_sealed",
            "narrative_ready",
            "waiting_for_clarification",
            "completed",
            "interaction_completed",
            "failed",
        }:
            self._stop_run_dispatch_heartbeat(dispatch_id)
            self._active_run_dispatches.pop(run_id, None)

    def _start_run_dispatch_heartbeat(
        self,
        *,
        dispatch_id: str,
        run_id: str,
        dispatch_owner_id: str,
        lease_epoch: int,
    ) -> None:
        if not (
            os.environ.get("WAJE_RUNTIME_DATABASE_URL")
            or os.environ.get("DATABASE_URL")
        ):
            return
        stop = threading.Event()
        self._run_dispatch_heartbeat_stops[dispatch_id] = stop
        interval = max(0.1, _run_dispatch_lease_ms() / 3000.0)

        def heartbeat() -> None:
            heartbeat_store: PostgresConversationStore | None = None
            try:
                heartbeat_store = PostgresConversationStore.from_env()
                while not stop.wait(interval):
                    if not heartbeat_store.renew_run_dispatch_lease(
                        dispatch_id=dispatch_id,
                        run_id=run_id,
                        dispatch_owner_id=dispatch_owner_id,
                        lease_epoch=lease_epoch,
                    ):
                        break
            finally:
                if heartbeat_store is not None:
                    close = getattr(heartbeat_store.connection, "close", None)
                    if callable(close):
                        close()

        threading.Thread(
            target=heartbeat,
            name=f"waje-run-dispatch-heartbeat-{dispatch_id}",
            daemon=True,
        ).start()

    def _stop_run_dispatch_heartbeat(self, dispatch_id: str) -> None:
        stop = self._run_dispatch_heartbeat_stops.pop(dispatch_id, None)
        if stop is not None:
            stop.set()

    def _lock_active_run_dispatch(
        self,
        *,
        dispatch_id: str,
        run_id: str,
        dispatch_owner_id: str,
        lease_epoch: int,
        expected_producer_kind: str | None = None,
    ) -> None:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        dispatch = self._fetchone(
            """
            /* generic_run_dispatch_owner_lock */
            SELECT dispatch_id, run_id, thread_id, dispatch_state, owner_id,
                   lease_epoch, lease_expires_at > now() AS lease_active,
                   producer_kind, scope_ref
            FROM waje_runtime.run_dispatches
            WHERE dispatch_id = %(dispatch_id)s
              AND run_id = %(run_id)s
            FOR UPDATE
            """,
            {"dispatch_id": dispatch_id, "run_id": run_id},
        )
        if (
            dispatch is None
            or str(_field(dispatch, "dispatch_id", 0) or "") != dispatch_id
            or str(_field(dispatch, "run_id", 1) or "") != run_id
            or str(_field(dispatch, "dispatch_state", 3) or "") != "running"
            or str(_field(dispatch, "owner_id", 4) or "") != dispatch_owner_id
            or int(_field(dispatch, "lease_epoch", 5) or 0) != lease_epoch
            or not bool(_field(dispatch, "lease_active", 6))
            or (
                expected_producer_kind is not None
                and (
                    str(_field(dispatch, "producer_kind", 7) or "")
                    != expected_producer_kind
                    or (
                        expected_producer_kind == "clarification_resolution"
                        and str(_field(dispatch, "scope_ref", 8) or "") != run_id
                    )
                )
            )
        ):
            raise EvidenceIntegrityError("run_dispatch_owner_lost")

    def _terminalize_active_run_dispatch(
        self,
        *,
        dispatch_id: str,
        run_id: str,
        dispatch_owner_id: str,
        lease_epoch: int,
        status: str,
        failure_reason: str = "",
    ) -> None:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        terminal = self._execute(
            """
            /* owned_run_dispatch_terminal_cas */
            UPDATE waje_runtime.run_dispatches
            SET dispatch_state = 'terminal',
                terminal_status = %(status)s,
                failure_reason = NULLIF(%(failure_reason)s, ''),
                lease_expires_at = NULL,
                heartbeat_at = now(), updated_at = now()
            WHERE dispatch_id = %(dispatch_id)s
              AND run_id = %(run_id)s
              AND dispatch_state = 'running'
              AND owner_id = %(owner_id)s
              AND lease_epoch = %(lease_epoch)s
            RETURNING dispatch_state
            """,
            {
                "dispatch_id": dispatch_id,
                "run_id": run_id,
                "owner_id": dispatch_owner_id,
                "lease_epoch": lease_epoch,
                "status": status,
                "failure_reason": failure_reason,
            },
            commit=False,
        ).fetchone()
        if terminal is None:
            raise EvidenceIntegrityError("run_dispatch_owner_lost")

    def finalize_run_failure(
        self,
        *,
        run_id: str,
        thread_id: str,
        turn_id: str,
        topic_id: str,
        request: Mapping[str, Any],
        failure_reason: str,
        failure_stage: str,
        failure_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        finalized_request = canonical_value(
            {
                **dict(request),
                "failure_reason": failure_reason,
                "failure_stage": failure_stage,
            }
        )
        primary_payload = canonical_value(
            {
                **dict(failure_payload),
                "failure_reason": failure_reason,
                "failure_stage": failure_stage,
            }
        )
        active_dispatch = self._active_run_dispatches.get(run_id)
        try:
            if active_dispatch is not None:
                self._lock_active_run_dispatch(
                    dispatch_id=active_dispatch[0],
                    run_id=run_id,
                    dispatch_owner_id=active_dispatch[1],
                    lease_epoch=active_dispatch[2],
                )
            current = self._fetchone(
                """
                /* analysis_run_status_transition_lock */
                SELECT status, thread_id, turn_id, topic_id, request
                FROM waje_runtime.analysis_runs
                WHERE run_id = %(run_id)s
                FOR UPDATE
                """,
                {"run_id": run_id},
            )
            if current is None:
                raise EvidenceIntegrityError("analysis_run_failure_source_missing")
            current_status = str(_field(current, "status", 0) or "")
            action = validate_run_status_transition(
                current_status=current_status,
                next_status="failed",
                current_thread_id=str(_field(current, "thread_id", 1) or ""),
                current_turn_id=str(_field(current, "turn_id", 2) or ""),
                current_topic_id=str(_field(current, "topic_id", 3) or ""),
                next_thread_id=thread_id,
                next_turn_id=turn_id,
                next_topic_id=topic_id,
                current_request=_json_value(_field(current, "request", 4)) or {},
                next_request=finalized_request,
            )
            if action == "replay":
                primary_rows = self._fetchall(
                    """
                    /* analysis_run_failure_primary_audit */
                    SELECT event_type, thread_id, topic_id, run_id, ref, payload
                    FROM waje_runtime.audit_events
                    WHERE run_id = %(run_id)s
                      AND event_type = %(failure_reason)s
                    FOR UPDATE
                    """,
                    {
                        "run_id": run_id,
                        "failure_reason": failure_reason,
                    },
                )
                if len(primary_rows) != 1:
                    raise EvidenceIntegrityError("analysis_run_failure_record_conflict")
                primary = primary_rows[0]
                if (
                    str(_field(primary, "event_type", 0) or "") != failure_reason
                    or str(_field(primary, "thread_id", 1) or "") != thread_id
                    or str(_field(primary, "topic_id", 2) or "") != topic_id
                    or str(_field(primary, "run_id", 3) or "") != run_id
                    or str(_field(primary, "ref", 4) or "") != run_id
                    or canonical_value(_json_value(_field(primary, "payload", 5)) or {})
                    != primary_payload
                ):
                    raise EvidenceIntegrityError("analysis_run_failure_record_conflict")
                if active_dispatch is not None:
                    self._terminalize_active_run_dispatch(
                        dispatch_id=active_dispatch[0],
                        run_id=run_id,
                        dispatch_owner_id=active_dispatch[1],
                        lease_epoch=active_dispatch[2],
                        status="failed",
                        failure_reason=failure_reason,
                    )
                self.connection.commit()
                if active_dispatch is not None:
                    self._stop_run_dispatch_heartbeat(active_dispatch[0])
                    self._active_run_dispatches.pop(run_id, None)
                return dict(finalized_request)

            updated = self._execute(
                """
                /* analysis_run_status_transition_cas */
                UPDATE waje_runtime.analysis_runs
                SET status = 'failed',
                    request = %(request)s::jsonb,
                    turn_id = %(turn_id)s,
                    topic_id = %(topic_id)s,
                    updated_at = now()
                WHERE run_id = %(run_id)s
                  AND status = %(current_status)s
                RETURNING status
                """,
                {
                    "run_id": run_id,
                    "request": _json(finalized_request),
                    "turn_id": turn_id or None,
                    "topic_id": topic_id or None,
                    "current_status": current_status,
                    "status": "failed",
                },
                commit=False,
            ).fetchone()
            if updated is None:
                raise EvidenceIntegrityError("analysis_run_status_transition_conflict")
            self._audit(
                "run_status_changed",
                thread_id=thread_id,
                topic_id=topic_id,
                run_id=run_id,
                ref=run_id,
                payload={"status": "failed"},
                commit=False,
            )
            self._audit(
                failure_reason,
                thread_id=thread_id,
                topic_id=topic_id,
                run_id=run_id,
                ref=run_id,
                payload=dict(primary_payload),
                commit=False,
            )
            if active_dispatch is not None:
                self._terminalize_active_run_dispatch(
                    dispatch_id=active_dispatch[0],
                    run_id=run_id,
                    dispatch_owner_id=active_dispatch[1],
                    lease_epoch=active_dispatch[2],
                    status="failed",
                    failure_reason=failure_reason,
                )
            self.connection.commit()
            if active_dispatch is not None:
                self._stop_run_dispatch_heartbeat(active_dispatch[0])
                self._active_run_dispatches.pop(run_id, None)
            return dict(finalized_request)
        except Exception:
            self.connection.rollback()
            raise

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

    def record_terminal_completion_conflict(
        self,
        *,
        run_id: str,
        thread_id: str,
        turn_id: str,
        topic_id: str,
        failure_reason: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        conflict_payload = canonical_value(
            {**dict(payload), "durable_run_status": "completed"}
        )
        try:
            current = self._fetchone(
                """
                /* terminal_completion_conflict_owner_lock */
                SELECT status, thread_id, turn_id, topic_id
                FROM waje_runtime.analysis_runs
                WHERE run_id = %(run_id)s
                FOR UPDATE
                """,
                {"run_id": run_id},
            )
            if (
                current is None
                or str(_field(current, "status", 0) or "") != "completed"
                or str(_field(current, "thread_id", 1) or "") != thread_id
                or str(_field(current, "turn_id", 2) or "") != turn_id
                or str(_field(current, "topic_id", 3) or "") != topic_id
            ):
                raise EvidenceIntegrityError(
                    "terminal_completion_conflict_owner_unproven"
                )
            rows = self._fetchall(
                """
                /* terminal_completion_conflict_audit */
                SELECT event_type, thread_id, topic_id, run_id, ref, payload
                FROM waje_runtime.audit_events
                WHERE run_id = %(run_id)s
                  AND event_type = %(failure_reason)s
                FOR UPDATE
                """,
                {"run_id": run_id, "failure_reason": failure_reason},
            )
            if rows:
                if len(rows) != 1:
                    raise EvidenceIntegrityError(
                        "terminal_completion_conflict_audit_mismatch"
                    )
                existing = rows[0]
                if (
                    str(_field(existing, "event_type", 0) or "") != failure_reason
                    or str(_field(existing, "thread_id", 1) or "") != thread_id
                    or str(_field(existing, "topic_id", 2) or "") != topic_id
                    or str(_field(existing, "run_id", 3) or "") != run_id
                    or str(_field(existing, "ref", 4) or "") != run_id
                    or canonical_value(
                        _json_value(_field(existing, "payload", 5)) or {}
                    )
                    != conflict_payload
                ):
                    raise EvidenceIntegrityError(
                        "terminal_completion_conflict_audit_mismatch"
                    )
                self.connection.commit()
                return dict(conflict_payload)
            self._audit(
                failure_reason,
                thread_id=thread_id,
                topic_id=topic_id,
                run_id=run_id,
                ref=run_id,
                payload=dict(conflict_payload),
                commit=False,
            )
            self.connection.commit()
            return dict(conflict_payload)
        except Exception:
            self.connection.rollback()
            raise

    def get_run_state(self, run_id: str) -> dict[str, Any] | None:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        row = self._fetchone(
            """
            /* analysis_run_state */
            SELECT run_id, thread_id, turn_id, topic_id, status, request
            FROM waje_runtime.analysis_runs
            WHERE run_id = %(run_id)s
            """,
            {"run_id": run_id},
        )
        if row is None:
            return None
        request = _json_value(_field(row, "request", 5))
        if not isinstance(request, Mapping):
            raise EvidenceIntegrityError("analysis_run_state_request_invalid")
        resolved_run_id = str(_field(row, "run_id", 0) or "")
        if resolved_run_id != run_id:
            raise EvidenceIntegrityError("analysis_run_state_owner_mismatch")
        return canonical_value(
            {
                "run_id": resolved_run_id,
                "thread_id": str(_field(row, "thread_id", 1) or ""),
                "turn_id": str(_field(row, "turn_id", 2) or ""),
                "topic_id": str(_field(row, "topic_id", 3) or ""),
                "status": str(_field(row, "status", 4) or ""),
                "request": request,
            }
        )

    def record_context_manifest(self, manifest: dict[str, Any]) -> None:
        self.save_context_manifest(manifest)

    def save_context_manifest(self, manifest: ContextManifest | dict[str, Any]) -> None:
        from bi_agent.runtime.context_manifest import (
            validated_context_manifest_record,
        )
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        payload = manifest.to_dict() if hasattr(manifest, "to_dict") else dict(manifest)
        runtime_signed = "manifest_digest" in payload
        if runtime_signed:
            payload = validated_context_manifest_record(payload)
        payload = canonical_value(payload)
        projection = {
            "manifest_id": str(payload["manifest_id"]),
            "thread_id": str(payload["thread_id"]),
            "turn_id": None if runtime_signed else payload.get("turn_id"),
            "topic_id": payload.get("topic_id") or None,
            "run_id": payload.get("run_id") or None,
            "can_support_claims": bool(payload.get("can_support_claims")),
            "items": (
                canonical_value(payload.get("sources") or ())
                if runtime_signed
                else payload
            ),
            "manifest_digest": (
                str(payload["manifest_digest"]) if runtime_signed else ""
            ),
            "payload": payload if runtime_signed else {},
        }
        try:
            self._execute(
                """
                SELECT pg_advisory_xact_lock(
                  hashtextextended(%(lock_key)s, 0)
                )
                """,
                {
                    "lock_key": (
                        f"context_manifest_publication:{projection['manifest_id']}"
                    )
                },
                commit=False,
            )
            existing = self._fetchone(
                """
                /* context_manifest_publication_preflight */
                SELECT manifest_id, thread_id, turn_id, topic_id, run_id,
                       can_support_claims, items, manifest_digest, payload
                FROM waje_runtime.context_manifests
                WHERE manifest_id = %(manifest_id)s
                FOR UPDATE
                """,
                {"manifest_id": projection["manifest_id"]},
            )
            if existing is not None:
                stored = {
                    "manifest_id": str(_field(existing, "manifest_id", 0)),
                    "thread_id": str(_field(existing, "thread_id", 1)),
                    "turn_id": _field(existing, "turn_id", 2),
                    "topic_id": _field(existing, "topic_id", 3),
                    "run_id": _field(existing, "run_id", 4),
                    "can_support_claims": bool(
                        _field(existing, "can_support_claims", 5)
                    ),
                    "items": canonical_value(_json_value(_field(existing, "items", 6))),
                    "manifest_digest": str(
                        _field(existing, "manifest_digest", 7) or ""
                    ),
                    "payload": canonical_value(
                        _json_value(_field(existing, "payload", 8)) or {}
                    ),
                }
                if canonical_value(stored) == canonical_value(projection):
                    self.connection.rollback()
                    return
                raise EvidenceIntegrityError("context_manifest_publication_conflict")
            self._insert_immutable(
                """
                INSERT INTO waje_runtime.context_manifests AS current(
                  manifest_id, thread_id, turn_id, topic_id, run_id,
                  can_support_claims, items, manifest_digest, payload
                ) VALUES (
                  %(manifest_id)s, %(thread_id)s, %(turn_id)s, %(topic_id)s,
                  %(run_id)s, %(can_support_claims)s, %(items)s::jsonb,
                  %(manifest_digest)s, %(payload)s::jsonb
                )
                ON CONFLICT (manifest_id) DO UPDATE
                SET manifest_id = current.manifest_id
                WHERE current.thread_id = EXCLUDED.thread_id
                  AND current.turn_id IS NOT DISTINCT FROM EXCLUDED.turn_id
                  AND current.topic_id IS NOT DISTINCT FROM EXCLUDED.topic_id
                  AND current.run_id IS NOT DISTINCT FROM EXCLUDED.run_id
                  AND current.can_support_claims = EXCLUDED.can_support_claims
                  AND current.items = EXCLUDED.items
                  AND current.manifest_digest = EXCLUDED.manifest_digest
                  AND current.payload = EXCLUDED.payload
                RETURNING manifest_id
                """,
                {
                    **projection,
                    "items": _json(projection["items"]),
                    "payload": _json(projection["payload"]),
                },
                collision="context_manifest",
            )
            self._audit(
                "context_manifest_recorded",
                thread_id=projection["thread_id"],
                run_id=projection["run_id"],
                topic_id=projection["topic_id"],
                ref=projection["manifest_id"],
                commit=False,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

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

    def save_intent_revision_transition(
        self,
        *,
        intent_revision: IntentRevision,
        transition: DurableTransition,
        input_payload: Mapping[str, Any],
        output_payload: Mapping[str, Any],
        accepted_attempt_refs: Sequence[str],
        affected_plan_fields: Sequence[str] = (),
        reason_ref: str = "intent_revision",
    ) -> dict[str, Any]:
        """Atomically accept an immutable intent revision and its node output."""
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
            canonical_value,
        )

        if (
            transition.run_attempt_id != intent_revision.run_attempt_id
            or transition.intent_revision_id != intent_revision.intent_revision_id
            or transition.acceptance_state != "accepted"
        ):
            raise EvidenceIntegrityError("intent_transition_authority_mismatch")
        if canonical_digest(input_payload) != transition.input_digest:
            raise EvidenceIntegrityError("transition_input_digest_mismatch")
        if canonical_digest(output_payload) != transition.output_digest:
            raise EvidenceIntegrityError("transition_output_digest_mismatch")
        if (
            isinstance(accepted_attempt_refs, (str, bytes))
            or len(tuple(accepted_attempt_refs)) != 1
        ):
            raise EvidenceIntegrityError("intent_provider_attempt_cardinality_invalid")
        normalized_attempt_refs = tuple(accepted_attempt_refs)
        payload = canonical_value(intent_revision.to_dict())
        try:
            self._lock_single_authority_run(intent_revision.run_attempt_id)
            active_rows = self._fetchall(
                """
                SELECT revision.intent_revision_id, revision.payload
                FROM waje_runtime.intent_revisions revision
                LEFT JOIN waje_runtime.intent_revision_supersessions supersession
                  ON supersession.superseded_intent_revision_id = revision.intent_revision_id
                WHERE revision.run_attempt_id = %(run_attempt_id)s
                  AND supersession.superseded_intent_revision_id IS NULL
                FOR UPDATE OF revision
                """,
                {"run_attempt_id": intent_revision.run_attempt_id},
            )
            if len(active_rows) > 1:
                raise EvidenceIntegrityError("active_intent_revision_ambiguous")
            active_id = (
                str(_field(active_rows[0], "intent_revision_id", 0) or "")
                if active_rows
                else ""
            )
            parent_id = intent_revision.supersedes_intent_revision_id or ""
            replaying_revision = active_id == intent_revision.intent_revision_id
            if active_id and not replaying_revision:
                raise EvidenceIntegrityError("active_intent_revision_conflict")
            parent_run_attempt_id = ""
            if parent_id and not replaying_revision:
                parent = self._fetchone(
                    """
                    SELECT parent.run_attempt_id
                    FROM waje_runtime.intent_revisions parent
                    LEFT JOIN waje_runtime.intent_revision_supersessions supersession
                      ON supersession.superseded_intent_revision_id = parent.intent_revision_id
                    WHERE parent.intent_revision_id = %(parent_id)s
                      AND supersession.superseded_intent_revision_id IS NULL
                    FOR UPDATE OF parent
                    """,
                    {"parent_id": parent_id},
                )
                parent_run_attempt_id = str(_field(parent, "run_attempt_id", 0) or "")
                if (
                    not parent_run_attempt_id
                    or parent_run_attempt_id == intent_revision.run_attempt_id
                ):
                    raise EvidenceIntegrityError("intent_revision_parent_not_active")

            inserted = self._execute(
                """
                INSERT INTO waje_runtime.intent_revisions(
                  intent_revision_id, run_attempt_id, supersedes_intent_revision_id,
                  original_user_text, content_digest, material_binding_digest,
                  schema_version, prompt_version, model_version, payload
                ) VALUES (
                  %(intent_revision_id)s, %(run_attempt_id)s,
                  %(supersedes_intent_revision_id)s, %(original_user_text)s,
                  %(content_digest)s, %(material_binding_digest)s,
                  %(schema_version)s, %(prompt_version)s, %(model_version)s,
                  %(payload)s::jsonb
                )
                ON CONFLICT DO NOTHING
                RETURNING intent_revision_id
                """,
                {
                    **payload,
                    "material_binding_digest": intent_revision.material_binding_digest,
                    "payload": _json(payload),
                },
                commit=False,
            ).fetchone()
            stored = self._fetchone(
                """
                SELECT payload
                FROM waje_runtime.intent_revisions
                WHERE intent_revision_id = %(intent_revision_id)s
                  AND run_attempt_id = %(run_attempt_id)s
                """,
                {
                    "intent_revision_id": intent_revision.intent_revision_id,
                    "run_attempt_id": intent_revision.run_attempt_id,
                },
            )
            if (
                stored is None
                or canonical_value(_json_value(_field(stored, "payload", 0)) or {})
                != payload
            ):
                raise EvidenceIntegrityError("intent_revision_immutable_conflict")

            if parent_id:
                supersession_body = canonical_value(
                    {
                        "superseded_intent_revision_id": parent_id,
                        "successor_intent_revision_id": intent_revision.intent_revision_id,
                        "affected_plan_fields": sorted(set(affected_plan_fields)),
                        "reason_ref": reason_ref,
                    }
                )
                supersession_digest = canonical_digest(supersession_body)
                supersession_id = "intent-supersession-" + supersession_digest[:24]
                self._execute(
                    """
                    INSERT INTO waje_runtime.intent_revision_supersessions(
                      supersession_id, superseded_intent_revision_id,
                      successor_intent_revision_id, affected_plan_fields,
                      reason_ref, content_digest
                    ) VALUES (
                      %(supersession_id)s, %(superseded_intent_revision_id)s,
                      %(successor_intent_revision_id)s,
                      %(affected_plan_fields)s::jsonb, %(reason_ref)s,
                      %(content_digest)s
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    {
                        **supersession_body,
                        "supersession_id": supersession_id,
                        "affected_plan_fields": _json(
                            supersession_body["affected_plan_fields"]
                        ),
                        "content_digest": supersession_digest,
                    },
                    commit=False,
                )
                persisted_supersession = self._fetchone(
                    """
                    SELECT superseded_intent_revision_id,
                           successor_intent_revision_id,
                           affected_plan_fields, reason_ref, content_digest
                    FROM waje_runtime.intent_revision_supersessions
                    WHERE supersession_id = %(supersession_id)s
                    """,
                    {"supersession_id": supersession_id},
                )
                stored_supersession = (
                    {
                        "superseded_intent_revision_id": str(
                            _field(
                                persisted_supersession,
                                "superseded_intent_revision_id",
                                0,
                            )
                            or ""
                        ),
                        "successor_intent_revision_id": str(
                            _field(
                                persisted_supersession,
                                "successor_intent_revision_id",
                                1,
                            )
                            or ""
                        ),
                        "affected_plan_fields": _json_value(
                            _field(persisted_supersession, "affected_plan_fields", 2)
                        ),
                        "reason_ref": str(
                            _field(persisted_supersession, "reason_ref", 3) or ""
                        ),
                    }
                    if persisted_supersession is not None
                    else None
                )
                if (
                    stored_supersession != supersession_body
                    or str(_field(persisted_supersession, "content_digest", 4) or "")
                    != supersession_digest
                ):
                    raise EvidenceIntegrityError(
                        "intent_revision_supersession_conflict"
                    )
                if parent_run_attempt_id:
                    parent_lifecycle = self._latest_lifecycle_state_locked(
                        parent_run_attempt_id
                    )
                    if parent_lifecycle is None:
                        superseded_lifecycle = LifecycleState.create(
                            run_attempt_id=parent_run_attempt_id,
                            execution_state="superseded",
                            interaction_state="superseded",
                            supersession_state="superseded",
                        )
                    elif parent_lifecycle.supersession_state == "active":
                        superseded_lifecycle = parent_lifecycle.transition(
                            execution_state="superseded",
                            interaction_state="superseded",
                            supersession_state="superseded",
                        )
                    else:
                        superseded_lifecycle = parent_lifecycle
                    self._append_lifecycle_state_locked(superseded_lifecycle)

                parent_ledger = self.load_decision_ledger(parent_id)
                revised_ledger = parent_ledger.supersede_for_revision(
                    intent_revision.intent_revision_id,
                    affected_plan_fields=frozenset(affected_plan_fields),
                )
                successor_records = tuple(
                    record
                    for record in revised_ledger.records
                    if record.intent_revision_id == intent_revision.intent_revision_id
                )
                for offset, decision in enumerate(successor_records, start=1):
                    ledger_position = parent_ledger.position + offset
                    self._execute(
                        """
                        INSERT INTO waje_runtime.decision_records(
                          ledger_position, decision_id, run_attempt_id,
                          intent_revision_id, slot_id, option_id,
                          source, status, materiality,
                          invalidated_by_revision_id,
                          supersedes_decision_id, content_digest, payload
                        ) VALUES (
                          %(ledger_position)s, %(decision_id)s,
                          %(run_attempt_id)s, %(intent_revision_id)s,
                          %(slot_id)s, %(option_id)s, %(source)s, %(status)s,
                          %(materiality)s, %(invalidated_by_revision_id)s,
                          %(supersedes_decision_id)s, %(content_digest)s,
                          %(payload)s::jsonb
                        )
                        ON CONFLICT DO NOTHING
                        """,
                        {
                            **decision.to_dict(),
                            "ledger_position": ledger_position,
                            "run_attempt_id": intent_revision.run_attempt_id,
                            "payload": _json(decision.to_dict()),
                        },
                        commit=False,
                    )
                    stored_decision = self._fetchone(
                        """
                        SELECT ledger_position, payload
                        FROM waje_runtime.decision_records
                        WHERE decision_id = %(decision_id)s
                        """,
                        {"decision_id": decision.decision_id},
                    )
                    if (
                        stored_decision is None
                        or int(
                            _field(
                                stored_decision,
                                "ledger_position",
                                0,
                            )
                            or 0
                        )
                        != ledger_position
                        or DecisionRecord.from_dict(
                            _json_value(_field(stored_decision, "payload", 1)) or {}
                        )
                        != decision
                    ):
                        raise EvidenceIntegrityError(
                            "intent_revision_decision_projection_conflict"
                        )
                expected_ledger_position = parent_ledger.position + len(
                    successor_records
                )
            else:
                expected_ledger_position = 0

            if transition.decision_ledger_position != expected_ledger_position:
                raise EvidenceIntegrityError(
                    "intent_transition_ledger_position_mismatch"
                )

            self._execute(
                """
                UPDATE waje_runtime.analysis_runs
                SET intent_revision_id = %(intent_revision_id)s
                WHERE run_id = %(run_attempt_id)s
                """,
                {
                    "intent_revision_id": intent_revision.intent_revision_id,
                    "run_attempt_id": intent_revision.run_attempt_id,
                },
                commit=False,
            )
            transition_status = self._save_transition_attempt_locked(
                transition=transition,
                input_payload=input_payload,
                output_payload=output_payload,
            )
            self.attempt_journal.bind_stage(
                run_attempt_id=intent_revision.run_attempt_id,
                transition_attempt_id=transition.attempt_id,
                stage_name="bind_intent",
                attempt_refs=normalized_attempt_refs,
                commit=False,
            )
            self.connection.commit()
            return {
                "intent_revision": payload,
                "transition": transition.to_dict(),
                "replayed": inserted is None and transition_status == "replayed",
            }
        except Exception:
            self.connection.rollback()
            raise

    def resolve_active_intent_revision(
        self, run_attempt_id: str
    ) -> IntentRevision | None:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        rows = self._fetchall(
            """
            SELECT revision.payload
            FROM waje_runtime.intent_revisions revision
            LEFT JOIN waje_runtime.intent_revision_supersessions supersession
              ON supersession.superseded_intent_revision_id = revision.intent_revision_id
            WHERE revision.run_attempt_id = %(run_attempt_id)s
              AND supersession.superseded_intent_revision_id IS NULL
            ORDER BY revision.created_at DESC, revision.intent_revision_id DESC
            LIMIT 2
            """,
            {"run_attempt_id": run_attempt_id},
        )
        if len(rows) > 1:
            raise EvidenceIntegrityError("active_intent_revision_ambiguous")
        if not rows:
            return None
        payload = _json_value(_field(rows[0], "payload", 0))
        try:
            revision = IntentRevision.from_dict(payload)
        except (TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("active_intent_revision_invalid") from exc
        if revision.run_attempt_id != run_attempt_id:
            raise EvidenceIntegrityError("active_intent_revision_owner_mismatch")
        return revision

    def save_plan_revision_transition(
        self,
        *,
        authority_context: AuthorityContext,
        planner_proposal: PlannerProposal,
        proposal_admission: ProposalAdmissionRecord,
        plan_revision: PlanRevision,
        transition: DurableTransition,
        input_payload: Mapping[str, Any],
        output_payload: Mapping[str, Any],
        accepted_attempt_refs: Sequence[str],
        plan_patch: Any | None = None,
    ) -> dict[str, Any]:
        """Atomically accept a pinned authority context and one plan revision."""
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
            canonical_value,
        )
        from bi_agent.runtime.authoritative_plan_result import (
            validate_proposal_admission_plan_closure,
            validate_planner_provider_audit_closure,
        )
        from bi_agent.runtime.claim_coverage import PlanPatch

        if (
            not isinstance(authority_context, AuthorityContext)
            or not isinstance(planner_proposal, PlannerProposal)
            or not isinstance(proposal_admission, ProposalAdmissionRecord)
            or not isinstance(plan_revision, PlanRevision)
            or not isinstance(transition, DurableTransition)
        ):
            raise EvidenceIntegrityError("plan_transition_record_type_invalid")
        try:
            rebuilt_records = (
                AuthorityContext.from_dict(authority_context.to_dict()),
                PlannerProposal.from_dict(planner_proposal.to_dict()),
                ProposalAdmissionRecord.from_dict(proposal_admission.to_dict()),
                PlanRevision.from_dict(plan_revision.to_dict()),
                DurableTransition.from_dict(transition.to_dict()),
            )
        except (TypeError, ValueError) as exc:
            raise EvidenceIntegrityError(
                "plan_transition_record_integrity_invalid"
            ) from exc
        if rebuilt_records != (
            authority_context,
            planner_proposal,
            proposal_admission,
            plan_revision,
            transition,
        ):
            raise EvidenceIntegrityError("plan_transition_record_integrity_invalid")
        run_attempt_id = authority_context.run_attempt_id
        if (
            planner_proposal.run_attempt_id != run_attempt_id
            or plan_revision.run_attempt_id != run_attempt_id
            or transition.run_attempt_id != run_attempt_id
            or planner_proposal.intent_revision_id
            != proposal_admission.intent_revision_id
            or planner_proposal.intent_revision_id != plan_revision.intent_revision_id
            or transition.intent_revision_id != plan_revision.intent_revision_id
            or planner_proposal.authority_context_ref
            != authority_context.authority_context_ref
            or proposal_admission.authority_context_ref
            != authority_context.authority_context_ref
            or plan_revision.authority_context_ref
            != authority_context.authority_context_ref
            or proposal_admission.planner_proposal_ref
            != planner_proposal.planner_proposal_id
            or plan_revision.planner_proposal_ref
            != planner_proposal.planner_proposal_id
            or plan_revision.proposal_admission_ref
            != proposal_admission.proposal_admission_id
            or planner_proposal.decision_refs != proposal_admission.decision_refs
            or planner_proposal.decision_refs != plan_revision.decision_refs
            or transition.node_name
            != (
                "compile_plan_patch"
                if plan_revision.supersedes_plan_revision_id is not None
                else "compile_authoritative_plan"
            )
            or transition.status != "succeeded"
            or transition.acceptance_state != "accepted"
            or transition.next_transition
            != (
                "phase03_plan_patch_bound"
                if plan_revision.supersedes_plan_revision_id is not None
                else "phase02_plan_bound"
            )
        ):
            raise EvidenceIntegrityError("plan_transition_authority_mismatch")
        if plan_revision.supersedes_plan_revision_id is None:
            if plan_patch is not None:
                raise EvidenceIntegrityError("plan_transition_patch_unexpected")
            plan_patch_ref = None
        else:
            if type(plan_patch) is not PlanPatch:
                raise EvidenceIntegrityError("plan_transition_patch_missing")
            plan_patch_ref = plan_patch.plan_patch_ref
            if (
                plan_patch.run_attempt_id != run_attempt_id
                or plan_patch.intent_revision_id != plan_revision.intent_revision_id
                or plan_patch.authority_context_ref
                != authority_context.authority_context_ref
                or plan_patch.source_plan_revision_id
                != plan_revision.supersedes_plan_revision_id
            ):
                raise EvidenceIntegrityError("plan_transition_patch_authority_mismatch")
        if (
            isinstance(accepted_attempt_refs, (str, bytes))
            or len(tuple(accepted_attempt_refs)) != 1
        ):
            raise EvidenceIntegrityError("planner_provider_attempt_cardinality_invalid")
        normalized_attempt_refs = tuple(accepted_attempt_refs)
        contract_versions = canonical_value(authority_context.contract_versions)
        if (
            canonical_value(proposal_admission.contract_versions) != contract_versions
            or canonical_value(plan_revision.contract_versions) != contract_versions
        ):
            raise EvidenceIntegrityError("plan_transition_contract_versions_mismatch")
        validate_proposal_admission_plan_closure(
            planner_proposal=planner_proposal,
            proposal_admission=proposal_admission,
            plan_revision=plan_revision,
        )
        expected_input = canonical_value(
            {
                "intent_revision_id": plan_revision.intent_revision_id,
                "decision_refs": list(plan_revision.decision_refs),
                "authority_context_ref": authority_context.authority_context_ref,
                "planner_proposal_ref": planner_proposal.planner_proposal_id,
                "proposal_admission_ref": proposal_admission.proposal_admission_id,
                "supersedes_plan_revision_id": (
                    plan_revision.supersedes_plan_revision_id
                ),
                "plan_patch_ref": plan_patch_ref,
            }
        )
        expected_output_records = canonical_value(
            {
                "authority_context": authority_context.to_dict(),
                "planner_proposal": planner_proposal.to_dict(),
                "proposal_admission_record": proposal_admission.to_dict(),
                "plan_revision": plan_revision.to_dict(),
            }
        )
        if not isinstance(output_payload, Mapping) or set(output_payload) != {
            *expected_output_records,
            "planner_llm_audit",
        }:
            raise EvidenceIntegrityError("plan_transition_output_payload_mismatch")
        planner_audit = output_payload.get("planner_llm_audit")
        validate_planner_provider_audit_closure(
            planner_audit=planner_audit,
            planner_proposal=planner_proposal,
            transition=transition,
        )
        expected_output = {
            **expected_output_records,
            "planner_llm_audit": canonical_value(planner_audit),
        }
        if canonical_value(input_payload) != expected_input:
            raise EvidenceIntegrityError("plan_transition_input_payload_mismatch")
        if canonical_value(output_payload) != expected_output:
            raise EvidenceIntegrityError("plan_transition_output_payload_mismatch")
        if canonical_digest(input_payload) != transition.input_digest:
            raise EvidenceIntegrityError("transition_input_digest_mismatch")
        if canonical_digest(output_payload) != transition.output_digest:
            raise EvidenceIntegrityError("transition_output_digest_mismatch")

        context_payload = canonical_value(authority_context.to_dict())
        proposal_payload = canonical_value(planner_proposal.to_dict())
        admission_payload = canonical_value(proposal_admission.to_dict())
        plan_payload = canonical_value(plan_revision.to_dict())
        plan_result_refs = _plan_result_refs(
            authority_context=authority_context,
            planner_proposal=planner_proposal,
            proposal_admission=proposal_admission,
            plan_revision=plan_revision,
            transition=transition,
            plan_patch_ref=plan_patch_ref,
        )
        try:
            self._lock_single_authority_run(run_attempt_id)
            current_head_row = self._fetchone(
                """
                SELECT transition_id
                FROM waje_runtime.workflow_transition_attempts
                WHERE run_attempt_id = %(run_attempt_id)s
                  AND acceptance_state = 'accepted'
                ORDER BY created_at DESC, attempt_id DESC
                LIMIT 1
                FOR UPDATE
                """,
                {"run_attempt_id": run_attempt_id},
            )
            current_head_id = (
                str(_field(current_head_row, "transition_id", 0) or "")
                if current_head_row is not None
                else ""
            )
            if current_head_id == transition.transition_id:
                pass
            elif transition.parent_transition_id != (current_head_id or None):
                raise EvidenceIntegrityError("plan_transition_parent_not_current_head")
            active_intent = self.resolve_active_intent_revision(run_attempt_id)
            if (
                active_intent is None
                or active_intent.intent_revision_id != plan_revision.intent_revision_id
            ):
                raise EvidenceIntegrityError("plan_intent_revision_not_active")
            ledger = self.load_decision_ledger(plan_revision.intent_revision_id)
            if (
                transition.decision_ledger_position != ledger.position
                or tuple(record.decision_id for record in ledger.active_records())
                != plan_revision.decision_refs
            ):
                raise EvidenceIntegrityError("plan_decision_ledger_mismatch")

            context_inserted = self._execute(
                """
                INSERT INTO waje_runtime.authority_contexts(
                  authority_context_ref, run_attempt_id, actual_as_of,
                  content_digest, payload
                ) VALUES (
                  %(authority_context_ref)s, %(run_attempt_id)s,
                  %(actual_as_of)s, %(content_digest)s, %(payload)s::jsonb
                )
                ON CONFLICT DO NOTHING
                RETURNING authority_context_ref
                """,
                {
                    **context_payload,
                    "payload": _json(context_payload),
                },
                commit=False,
            ).fetchone()
            stored_context = self._fetchone(
                """
                SELECT authority_context_ref, payload
                FROM waje_runtime.authority_contexts
                WHERE run_attempt_id = %(run_attempt_id)s
                FOR UPDATE
                """,
                {"run_attempt_id": run_attempt_id},
            )
            if (
                stored_context is None
                or str(_field(stored_context, "authority_context_ref", 0) or "")
                != authority_context.authority_context_ref
                or canonical_value(
                    _json_value(_field(stored_context, "payload", 1)) or {}
                )
                != context_payload
            ):
                raise EvidenceIntegrityError("authority_context_conflict")

            active_plan_rows = self._fetchall(
                """
                SELECT plan.plan_revision_id, plan.intent_revision_id,
                       plan.authority_context_ref, plan.payload
                FROM waje_runtime.plan_revisions plan
                LEFT JOIN waje_runtime.plan_revision_supersessions supersession
                  ON supersession.superseded_plan_revision_id = plan.plan_revision_id
                WHERE plan.run_attempt_id = %(run_attempt_id)s
                  AND supersession.superseded_plan_revision_id IS NULL
                FOR UPDATE OF plan
                """,
                {"run_attempt_id": run_attempt_id},
            )
            if len(active_plan_rows) > 1:
                raise EvidenceIntegrityError("active_plan_revision_ambiguous")
            active_plan_id = (
                str(_field(active_plan_rows[0], "plan_revision_id", 0) or "")
                if active_plan_rows
                else ""
            )
            previous_active_plan = None
            if active_plan_rows:
                try:
                    previous_active_plan = PlanRevision.from_dict(
                        _json_value(_field(active_plan_rows[0], "payload", 3)) or {}
                    )
                except (TypeError, ValueError) as exc:
                    raise EvidenceIntegrityError(
                        "active_plan_revision_invalid"
                    ) from exc
                if (
                    previous_active_plan.plan_revision_id != active_plan_id
                    or previous_active_plan.run_attempt_id != run_attempt_id
                ):
                    raise EvidenceIntegrityError("active_plan_revision_invalid")
            replaying_plan = active_plan_id == plan_revision.plan_revision_id
            parent_plan_id = plan_revision.supersedes_plan_revision_id or ""
            if active_plan_id and not replaying_plan:
                if not parent_plan_id or parent_plan_id != active_plan_id:
                    raise EvidenceIntegrityError("active_plan_revision_conflict")
            elif parent_plan_id and not replaying_plan:
                raise EvidenceIntegrityError("plan_superseded_revision_not_active")
            if parent_plan_id and not replaying_plan:
                parent = self._fetchone(
                    """
                    SELECT run_attempt_id, intent_revision_id,
                           authority_context_ref
                    FROM waje_runtime.plan_revisions
                    WHERE plan_revision_id = %(plan_revision_id)s
                    FOR UPDATE
                    """,
                    {"plan_revision_id": parent_plan_id},
                )
                if (
                    parent is None
                    or str(_field(parent, "run_attempt_id", 0) or "") != run_attempt_id
                    or str(_field(parent, "intent_revision_id", 1) or "")
                    != plan_revision.intent_revision_id
                    or str(_field(parent, "authority_context_ref", 2) or "")
                    != authority_context.authority_context_ref
                ):
                    raise EvidenceIntegrityError("plan_supersession_authority_mismatch")
                coverage_parent = self._fetchone(
                    """
                    SELECT node_name, next_transition, output_payload
                    FROM waje_runtime.workflow_transition_attempts
                    WHERE transition_id = %(transition_id)s
                      AND run_attempt_id = %(run_attempt_id)s
                      AND status = 'succeeded'
                      AND acceptance_state = 'accepted'
                    """,
                    {
                        "transition_id": transition.parent_transition_id,
                        "run_attempt_id": run_attempt_id,
                    },
                )
                coverage_output = (
                    _json_value(_field(coverage_parent, "output_payload", 2)) or {}
                    if coverage_parent is not None
                    else {}
                )
                persisted_patch = (
                    coverage_output.get("plan_patch")
                    if isinstance(coverage_output, Mapping)
                    else None
                )
                persisted_evaluation = (
                    coverage_output.get("claim_coverage_evaluation")
                    if isinstance(coverage_output, Mapping)
                    else None
                )
                persisted_decision = (
                    coverage_output.get("plan_expansion_decision")
                    if isinstance(coverage_output, Mapping)
                    else None
                )
                if (
                    coverage_parent is None
                    or str(_field(coverage_parent, "node_name", 0) or "")
                    != "evaluate_claim_coverage"
                    or str(_field(coverage_parent, "next_transition", 1) or "")
                    != "compile_plan_patch"
                    or canonical_value(persisted_patch)
                    != canonical_value(plan_patch.to_dict())
                    or not isinstance(persisted_evaluation, Mapping)
                    or persisted_evaluation.get("source_plan_revision_id")
                    != parent_plan_id
                    or not isinstance(persisted_decision, Mapping)
                    or persisted_decision.get("decision") != "patch"
                    or persisted_decision.get("decision_ref")
                    != plan_patch.plan_expansion_decision_ref
                ):
                    raise EvidenceIntegrityError("plan_patch_parent_transition_invalid")

            proposal_inserted = self._execute(
                """
                INSERT INTO waje_runtime.planner_proposals(
                  planner_proposal_id, run_attempt_id, intent_revision_id,
                  authority_context_ref, content_digest, schema_version,
                  prompt_version, model_version, payload
                ) VALUES (
                  %(planner_proposal_id)s, %(run_attempt_id)s,
                  %(intent_revision_id)s, %(authority_context_ref)s,
                  %(content_digest)s, %(schema_version)s, %(prompt_version)s,
                  %(model_version)s, %(payload)s::jsonb
                )
                ON CONFLICT DO NOTHING
                RETURNING planner_proposal_id
                """,
                {
                    **proposal_payload,
                    "payload": _json(proposal_payload),
                },
                commit=False,
            ).fetchone()
            stored_proposal = self._fetchone(
                """
                SELECT payload FROM waje_runtime.planner_proposals
                WHERE planner_proposal_id = %(planner_proposal_id)s
                  AND run_attempt_id = %(run_attempt_id)s
                """,
                {
                    "planner_proposal_id": planner_proposal.planner_proposal_id,
                    "run_attempt_id": run_attempt_id,
                },
            )
            if (
                stored_proposal is None
                or canonical_value(
                    _json_value(_field(stored_proposal, "payload", 0)) or {}
                )
                != proposal_payload
            ):
                raise EvidenceIntegrityError("planner_proposal_immutable_conflict")

            admission_inserted = self._execute(
                """
                INSERT INTO waje_runtime.proposal_admission_records(
                  proposal_admission_id, planner_proposal_ref,
                  intent_revision_id, authority_context_ref, compiler_version,
                  content_digest, payload
                ) VALUES (
                  %(proposal_admission_id)s, %(planner_proposal_ref)s,
                  %(intent_revision_id)s, %(authority_context_ref)s,
                  %(compiler_version)s, %(content_digest)s, %(payload)s::jsonb
                )
                ON CONFLICT DO NOTHING
                RETURNING proposal_admission_id
                """,
                {
                    **admission_payload,
                    "payload": _json(admission_payload),
                },
                commit=False,
            ).fetchone()
            stored_admission = self._fetchone(
                """
                SELECT payload FROM waje_runtime.proposal_admission_records
                WHERE proposal_admission_id = %(proposal_admission_id)s
                """,
                {"proposal_admission_id": (proposal_admission.proposal_admission_id)},
            )
            if (
                stored_admission is None
                or canonical_value(
                    _json_value(_field(stored_admission, "payload", 0)) or {}
                )
                != admission_payload
            ):
                raise EvidenceIntegrityError("proposal_admission_immutable_conflict")

            plan_inserted = self._execute(
                """
                INSERT INTO waje_runtime.plan_revisions(
                  plan_revision_id, run_attempt_id, intent_revision_id,
                  authority_context_ref, planner_proposal_ref,
                  proposal_admission_ref, supersedes_plan_revision_id,
                  content_digest, payload
                ) VALUES (
                  %(plan_revision_id)s, %(run_attempt_id)s,
                  %(intent_revision_id)s, %(authority_context_ref)s,
                  %(planner_proposal_ref)s, %(proposal_admission_ref)s,
                  %(supersedes_plan_revision_id)s, %(content_digest)s,
                  %(payload)s::jsonb
                )
                ON CONFLICT DO NOTHING
                RETURNING plan_revision_id
                """,
                {
                    **plan_payload,
                    "payload": _json(plan_payload),
                },
                commit=False,
            ).fetchone()
            stored_plan = self._fetchone(
                """
                SELECT payload FROM waje_runtime.plan_revisions
                WHERE plan_revision_id = %(plan_revision_id)s
                  AND run_attempt_id = %(run_attempt_id)s
                """,
                {
                    "plan_revision_id": plan_revision.plan_revision_id,
                    "run_attempt_id": run_attempt_id,
                },
            )
            if (
                stored_plan is None
                or canonical_value(_json_value(_field(stored_plan, "payload", 0)) or {})
                != plan_payload
            ):
                raise EvidenceIntegrityError("plan_revision_immutable_conflict")

            if parent_plan_id:
                supersession_body = canonical_value(
                    {
                        "superseded_plan_revision_id": parent_plan_id,
                        "successor_plan_revision_id": plan_revision.plan_revision_id,
                        "authority_context_ref": (
                            authority_context.authority_context_ref
                        ),
                    }
                )
                supersession_digest = canonical_digest(supersession_body)
                supersession_id = "plan-supersession-" + supersession_digest[:24]
                self._execute(
                    """
                    INSERT INTO waje_runtime.plan_revision_supersessions(
                      supersession_id, superseded_plan_revision_id,
                      successor_plan_revision_id, authority_context_ref,
                      content_digest
                    ) VALUES (
                      %(supersession_id)s, %(superseded_plan_revision_id)s,
                      %(successor_plan_revision_id)s,
                      %(authority_context_ref)s, %(content_digest)s
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    {
                        **supersession_body,
                        "supersession_id": supersession_id,
                        "content_digest": supersession_digest,
                    },
                    commit=False,
                )
                stored_supersession = self._fetchone(
                    """
                    SELECT superseded_plan_revision_id,
                           successor_plan_revision_id,
                           authority_context_ref, content_digest
                    FROM waje_runtime.plan_revision_supersessions
                    WHERE supersession_id = %(supersession_id)s
                    """,
                    {"supersession_id": supersession_id},
                )
                if (
                    stored_supersession is None
                    or {
                        "superseded_plan_revision_id": str(
                            _field(
                                stored_supersession,
                                "superseded_plan_revision_id",
                                0,
                            )
                            or ""
                        ),
                        "successor_plan_revision_id": str(
                            _field(
                                stored_supersession,
                                "successor_plan_revision_id",
                                1,
                            )
                            or ""
                        ),
                        "authority_context_ref": str(
                            _field(
                                stored_supersession,
                                "authority_context_ref",
                                2,
                            )
                            or ""
                        ),
                    }
                    != supersession_body
                    or str(_field(stored_supersession, "content_digest", 3) or "")
                    != supersession_digest
                ):
                    raise EvidenceIntegrityError("plan_revision_supersession_conflict")

            transition_status = self._save_transition_attempt_locked(
                transition=transition,
                input_payload=input_payload,
                output_payload=output_payload,
            )
            self.attempt_journal.bind_stage(
                run_attempt_id=run_attempt_id,
                transition_attempt_id=transition.attempt_id,
                stage_name=transition.node_name,
                attempt_refs=normalized_attempt_refs,
                commit=False,
            )
            previous_plan_result_refs = None
            if parent_plan_id and previous_active_plan is not None:
                previous_transition = self.load_plan_revision_transition(
                    previous_active_plan.plan_revision_id
                )
                if not isinstance(previous_transition, Mapping):
                    raise EvidenceIntegrityError("previous_plan_transition_missing")
                previous_transition_record = previous_transition.get("transition")
                previous_transition_input = previous_transition.get("input_payload")
                if not isinstance(
                    previous_transition_record, DurableTransition
                ) or not isinstance(previous_transition_input, Mapping):
                    raise EvidenceIntegrityError("previous_plan_transition_invalid")
                previous_plan_result_refs = _plan_result_refs_from_revision(
                    plan_revision=previous_active_plan,
                    accepted_transition_id=(previous_transition_record.transition_id),
                    plan_patch_ref=previous_transition_input.get("plan_patch_ref"),
                )
            self._persist_plan_result_refs_locked(
                run_attempt_id=run_attempt_id,
                plan_result_refs=plan_result_refs,
                transition_replayed=transition_status == "replayed",
                previous_plan_result_refs=previous_plan_result_refs,
                plan_patch_ref=plan_patch_ref,
            )
            self.connection.commit()
            return {
                "authority_context": context_payload,
                "planner_proposal": proposal_payload,
                "proposal_admission": admission_payload,
                "plan_revision": plan_payload,
                "transition": transition.to_dict(),
                "replayed": (
                    context_inserted is None
                    and proposal_inserted is None
                    and admission_inserted is None
                    and plan_inserted is None
                    and transition_status == "replayed"
                ),
            }
        except Exception:
            self.connection.rollback()
            raise

    def load_plan_revision_transition(
        self, plan_revision_id: str
    ) -> Mapping[str, Any] | None:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        rows = self._fetchall(
            """
            SELECT run_attempt_id, node_name, input_digest
            FROM waje_runtime.workflow_transition_attempts
            WHERE status = 'succeeded'
              AND acceptance_state = 'accepted'
              AND node_name IN (
                'compile_authoritative_plan', 'compile_plan_patch'
              )
              AND output_payload #>> '{plan_revision,plan_revision_id}'
                  = %(plan_revision_id)s
            ORDER BY created_at DESC, attempt_id DESC
            LIMIT 2
            """,
            {"plan_revision_id": plan_revision_id},
        )
        if len(rows) > 1:
            raise EvidenceIntegrityError("plan_transition_ambiguous")
        if not rows:
            return None
        return self.load_accepted_transition(
            run_attempt_id=str(_field(rows[0], "run_attempt_id", 0) or ""),
            node_name=str(_field(rows[0], "node_name", 1) or ""),
            input_digest=str(_field(rows[0], "input_digest", 2) or ""),
        )

    def load_authority_context(self, run_attempt_id: str) -> AuthorityContext | None:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        row = self._fetchone(
            """
            SELECT payload FROM waje_runtime.authority_contexts
            WHERE run_attempt_id = %(run_attempt_id)s
            """,
            {"run_attempt_id": run_attempt_id},
        )
        if row is None:
            return None
        try:
            context = AuthorityContext.from_dict(
                _json_value(_field(row, "payload", 0)) or {}
            )
        except (TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("authority_context_invalid") from exc
        if context.run_attempt_id != run_attempt_id:
            raise EvidenceIntegrityError("authority_context_owner_mismatch")
        return context

    def resolve_active_plan_revision(self, run_attempt_id: str) -> PlanRevision | None:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        rows = self._fetchall(
            """
            SELECT plan.payload
            FROM waje_runtime.plan_revisions plan
            LEFT JOIN waje_runtime.plan_revision_supersessions supersession
              ON supersession.superseded_plan_revision_id = plan.plan_revision_id
            WHERE plan.run_attempt_id = %(run_attempt_id)s
              AND supersession.superseded_plan_revision_id IS NULL
            ORDER BY plan.created_at DESC, plan.plan_revision_id DESC
            LIMIT 2
            """,
            {"run_attempt_id": run_attempt_id},
        )
        if len(rows) > 1:
            raise EvidenceIntegrityError("active_plan_revision_ambiguous")
        if not rows:
            return None
        try:
            plan = PlanRevision.from_dict(
                _json_value(_field(rows[0], "payload", 0)) or {}
            )
        except (TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("active_plan_revision_invalid") from exc
        if plan.run_attempt_id != run_attempt_id:
            raise EvidenceIntegrityError("active_plan_revision_owner_mismatch")
        return plan

    def load_capability_outcome(
        self,
        plan_revision_id: str,
        task_id: str,
    ):
        from bi_agent.runtime.capability_authority import (
            CapabilityAttempt,
            CapabilityOutcome,
            EvidenceLedgerEntry,
            FailureRecord,
        )
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        row = self._fetchone(
            """
            SELECT attempt.payload AS attempt_payload,
                   outcome.payload AS outcome_payload
            FROM waje_runtime.capability_outcomes outcome
            JOIN waje_runtime.capability_task_attempts attempt
              ON attempt.attempt_id = outcome.attempt_id
            WHERE outcome.plan_revision_id = %(plan_revision_id)s
              AND outcome.task_id = %(task_id)s
            """,
            {
                "plan_revision_id": plan_revision_id,
                "task_id": task_id,
            },
        )
        if row is None:
            return None
        try:
            attempt = CapabilityAttempt.from_dict(
                _json_value(_field(row, "attempt_payload", 0)) or {}
            )
            outcome = CapabilityOutcome.from_dict(
                _json_value(_field(row, "outcome_payload", 1)) or {}
            )
            evidence = tuple(
                EvidenceLedgerEntry.from_dict(
                    _json_value(_field(item, "payload", 0)) or {}
                )
                for item in self._fetchall(
                    """
                    SELECT payload
                    FROM waje_runtime.capability_evidence_ledger_entries
                    WHERE outcome_ref = %(outcome_ref)s
                    ORDER BY entry_ref
                    """,
                    {"outcome_ref": outcome.outcome_ref},
                )
            )
            failures = tuple(
                FailureRecord.from_dict(_json_value(_field(item, "payload", 0)) or {})
                for item in self._fetchall(
                    """
                    SELECT payload
                    FROM waje_runtime.capability_failure_records
                    WHERE attempt_id = %(attempt_id)s
                    ORDER BY failure_ref
                    """,
                    {"attempt_id": attempt.attempt_id},
                )
            )
        except (TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("capability_outcome_bundle_invalid") from exc
        if (
            attempt.plan_revision_id != plan_revision_id
            or attempt.task_id != task_id
            or outcome.plan_revision_id != plan_revision_id
            or outcome.task_id != task_id
            or outcome.attempt_id != attempt.attempt_id
            or set(outcome.evidence_refs) != {item.evidence_ref for item in evidence}
            or ({outcome.failure_ref} if outcome.failure_ref else set())
            != {item.failure_ref for item in failures}
        ):
            raise EvidenceIntegrityError("capability_outcome_bundle_invalid")
        self._validate_accepted_capability_call(
            attempt,
            outcome,
            evidence,
            failures,
        )
        return attempt, outcome, evidence, failures

    def accept_capability_outcome(
        self,
        attempt: Any,
        outcome: Any,
        evidence_entries: Sequence[Any],
        failures: Sequence[Any],
        settlement_authority: Any,
    ):
        from bi_agent.runtime.capability_authority import (
            CapabilityAttempt,
            CapabilityOutcome,
            EvidenceLedgerEntry,
            FailureRecord,
        )
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
            canonical_value,
        )
        from bi_agent.runtime.runtime_persistence import (
            CapabilitySettlementAuthority,
        )

        try:
            rebuilt_attempt = CapabilityAttempt.from_dict(attempt.to_dict())
            rebuilt_outcome = CapabilityOutcome.from_dict(outcome.to_dict())
            rebuilt_evidence = tuple(
                EvidenceLedgerEntry.from_dict(item.to_dict())
                for item in evidence_entries
            )
            rebuilt_failures = tuple(
                FailureRecord.from_dict(item.to_dict()) for item in failures
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("capability_outcome_bundle_invalid") from exc
        if type(settlement_authority) is not CapabilitySettlementAuthority:
            raise EvidenceIntegrityError(
                "capability_outcome_settlement_authority_invalid"
            )
        settlement_authority = settlement_authority.revalidated()
        binding_refs = {
            item.binding_record_ref
            for item in rebuilt_evidence
            if item.binding_record_ref is not None
        }
        bindings_by_ref = {
            item.record_ref: item
            for item in settlement_authority.capability_binding_records
        }
        if (
            rebuilt_attempt != attempt
            or rebuilt_outcome != outcome
            or rebuilt_evidence != tuple(evidence_entries)
            or rebuilt_failures != tuple(failures)
            or outcome.run_attempt_id != attempt.run_attempt_id
            or outcome.plan_revision_id != attempt.plan_revision_id
            or outcome.task_id != attempt.task_id
            or outcome.attempt_id != attempt.attempt_id
            or set(outcome.evidence_refs)
            != {item.evidence_ref for item in evidence_entries}
            or any(
                item.run_attempt_id != attempt.run_attempt_id
                or item.plan_revision_id != attempt.plan_revision_id
                or item.task_id != attempt.task_id
                or item.outcome_ref != outcome.outcome_ref
                for item in evidence_entries
            )
            or ({outcome.failure_ref} if outcome.failure_ref else set())
            != {item.failure_ref for item in failures}
            or any(
                item.run_attempt_id != attempt.run_attempt_id
                or item.plan_revision_id != attempt.plan_revision_id
                or item.task_id != attempt.task_id
                or item.attempt_id != attempt.attempt_id
                for item in failures
            )
            or settlement_authority.run_id != attempt.run_attempt_id
            or set(bindings_by_ref) != binding_refs
        ):
            raise EvidenceIntegrityError("capability_outcome_bundle_invalid")
        for entry in rebuilt_evidence:
            if entry.binding_record_ref is None:
                continue
            binding = bindings_by_ref[entry.binding_record_ref]
            if entry.maximum_claim_strength != binding.maximum_claim_strength:
                raise EvidenceIntegrityError(
                    "capability_outcome_binding_claim_ceiling_mismatch"
                )
            if set(entry.result_refs) != {
                *binding.result_refs,
                *binding.validation_result_refs,
            }:
                raise EvidenceIntegrityError(
                    "capability_outcome_binding_result_membership_mismatch"
                )
            if set(entry.completeness_report_refs) != {
                *binding.completeness_report_refs,
                *binding.validation_completeness_report_refs,
            }:
                raise EvidenceIntegrityError(
                    "capability_outcome_binding_completeness_membership_mismatch"
                )

        try:
            self._lock_single_authority_run(attempt.run_attempt_id)
            active_plan = self.resolve_active_plan_revision(attempt.run_attempt_id)
            if (
                active_plan is None
                or active_plan.plan_revision_id != attempt.plan_revision_id
                or active_plan.intent_revision_id != attempt.intent_revision_id
            ):
                raise EvidenceIntegrityError("capability_outcome_plan_not_active")
            task = next(
                (
                    item
                    for item in active_plan.capability_tasks
                    if item.task_id == attempt.task_id
                ),
                None,
            )
            if (
                task is None
                or task.idempotency_key != attempt.task_idempotency_key
                or canonical_digest(task.normalized_input_refs)
                != attempt.normalized_input_digest
                or canonical_digest(active_plan.contract_versions)
                != attempt.contract_versions_digest
                or canonical_digest(
                    {"authority_context_ref": (active_plan.authority_context_ref)}
                )
                != attempt.release_set_digest
            ):
                raise EvidenceIntegrityError("capability_attempt_plan_closure_invalid")
            self._validate_accepted_capability_call(
                attempt,
                outcome,
                rebuilt_evidence,
                rebuilt_failures,
            )
            if any(
                binding.capability_id != task.capability_id
                for binding in bindings_by_ref.values()
            ):
                raise EvidenceIntegrityError(
                    "capability_outcome_binding_capability_mismatch"
                )
            if task.dependency_task_ids:
                dependency_rows = self._fetchall(
                    """
                    SELECT task_id
                    FROM waje_runtime.capability_outcomes
                    WHERE plan_revision_id = %(plan_revision_id)s
                      AND task_id = ANY(%(dependency_task_ids)s)
                    """,
                    {
                        "plan_revision_id": attempt.plan_revision_id,
                        "dependency_task_ids": list(task.dependency_task_ids),
                    },
                )
                if {
                    str(_field(item, "task_id", 0) or "") for item in dependency_rows
                } != set(task.dependency_task_ids):
                    raise EvidenceIntegrityError(
                        "capability_outcome_dependency_missing"
                    )

            self._insert_analysis_authority_graph(
                run_id=settlement_authority.run_id,
                analysis_contract=settlement_authority.analysis_contract,
                query_contracts=settlement_authority.query_contracts,
                query_execution_records=(settlement_authority.query_execution_records),
                rows_records=settlement_authority.rows_records,
                snapshot_records=settlement_authority.snapshot_records,
                completeness_records=(settlement_authority.completeness_records),
                capability_binding_records=(
                    settlement_authority.capability_binding_records
                ),
            )

            existing = self.load_capability_outcome(
                attempt.plan_revision_id,
                attempt.task_id,
            )
            requested = (
                attempt,
                outcome,
                tuple(evidence_entries),
                tuple(failures),
            )
            if existing is not None:
                if existing != requested:
                    raise EvidenceIntegrityError(
                        "capability_outcome_immutable_conflict"
                    )
                self.connection.rollback()
                return existing

            attempt_payload = canonical_value(attempt.to_dict())
            self._insert_immutable(
                """
                INSERT INTO waje_runtime.capability_task_attempts AS current(
                  attempt_id, run_attempt_id, intent_revision_id,
                  plan_revision_id, task_id, task_idempotency_key,
                  execution_attempt, normalized_input_digest,
                  release_set_digest, contract_versions_digest,
                  input_digest, content_digest, payload
                ) VALUES (
                  %(attempt_id)s, %(run_attempt_id)s, %(intent_revision_id)s,
                  %(plan_revision_id)s, %(task_id)s,
                  %(task_idempotency_key)s, %(execution_attempt)s,
                  %(normalized_input_digest)s, %(release_set_digest)s,
                  %(contract_versions_digest)s, %(input_digest)s,
                  %(content_digest)s, %(payload)s::jsonb
                )
                ON CONFLICT (attempt_id) DO UPDATE
                SET attempt_id = current.attempt_id
                WHERE current.run_attempt_id = EXCLUDED.run_attempt_id
                  AND current.intent_revision_id = EXCLUDED.intent_revision_id
                  AND current.plan_revision_id = EXCLUDED.plan_revision_id
                  AND current.task_id = EXCLUDED.task_id
                  AND current.task_idempotency_key = EXCLUDED.task_idempotency_key
                  AND current.execution_attempt = EXCLUDED.execution_attempt
                  AND current.normalized_input_digest = EXCLUDED.normalized_input_digest
                  AND current.release_set_digest = EXCLUDED.release_set_digest
                  AND current.contract_versions_digest = EXCLUDED.contract_versions_digest
                  AND current.input_digest = EXCLUDED.input_digest
                  AND current.content_digest = EXCLUDED.content_digest
                  AND current.payload = EXCLUDED.payload
                RETURNING attempt_id
                """,
                {**attempt_payload, "payload": _json(attempt_payload)},
                collision="capability_attempt",
            )
            for failure in failures:
                payload = canonical_value(failure.to_dict())
                self._insert_immutable(
                    """
                    INSERT INTO waje_runtime.capability_failure_records AS current(
                      failure_ref, run_attempt_id, plan_revision_id, task_id,
                      attempt_id, layer, kind, integrity_level, retryability,
                      content_digest, payload
                    ) VALUES (
                      %(failure_ref)s, %(run_attempt_id)s,
                      %(plan_revision_id)s, %(task_id)s, %(attempt_id)s,
                      %(layer)s, %(kind)s, %(integrity_level)s,
                      %(retryability)s, %(content_digest)s, %(payload)s::jsonb
                    )
                    ON CONFLICT (failure_ref) DO UPDATE
                    SET failure_ref = current.failure_ref
                    WHERE current.run_attempt_id = EXCLUDED.run_attempt_id
                      AND current.plan_revision_id = EXCLUDED.plan_revision_id
                      AND current.task_id = EXCLUDED.task_id
                      AND current.attempt_id = EXCLUDED.attempt_id
                      AND current.layer = EXCLUDED.layer
                      AND current.kind = EXCLUDED.kind
                      AND current.integrity_level = EXCLUDED.integrity_level
                      AND current.retryability = EXCLUDED.retryability
                      AND current.content_digest = EXCLUDED.content_digest
                      AND current.payload = EXCLUDED.payload
                    RETURNING failure_ref
                    """,
                    {**payload, "payload": _json(payload)},
                    collision="capability_failure_record",
                )
            outcome_payload = canonical_value(outcome.to_dict())
            self._insert_immutable(
                """
                INSERT INTO waje_runtime.capability_outcomes AS current(
                  outcome_ref, attempt_id, run_attempt_id, plan_revision_id,
                  task_id, status, retryability, failure_ref, input_digest,
                  output_digest, content_digest, payload
                ) VALUES (
                  %(outcome_ref)s, %(attempt_id)s, %(run_attempt_id)s,
                  %(plan_revision_id)s, %(task_id)s, %(status)s,
                  %(retryability)s, %(failure_ref)s, %(input_digest)s,
                  %(output_digest)s, %(content_digest)s, %(payload)s::jsonb
                )
                ON CONFLICT (outcome_ref) DO UPDATE
                SET outcome_ref = current.outcome_ref
                WHERE current.attempt_id = EXCLUDED.attempt_id
                  AND current.run_attempt_id = EXCLUDED.run_attempt_id
                  AND current.plan_revision_id = EXCLUDED.plan_revision_id
                  AND current.task_id = EXCLUDED.task_id
                  AND current.status = EXCLUDED.status
                  AND current.retryability = EXCLUDED.retryability
                  AND current.failure_ref IS NOT DISTINCT FROM EXCLUDED.failure_ref
                  AND current.input_digest = EXCLUDED.input_digest
                  AND current.output_digest = EXCLUDED.output_digest
                  AND current.content_digest = EXCLUDED.content_digest
                  AND current.payload = EXCLUDED.payload
                RETURNING outcome_ref
                """,
                {**outcome_payload, "payload": _json(outcome_payload)},
                collision="capability_outcome",
            )
            for entry in evidence_entries:
                payload = canonical_value(entry.to_dict())
                self._insert_immutable(
                    """
                    INSERT INTO waje_runtime.capability_evidence_ledger_entries AS current(
                      entry_ref, run_attempt_id, authority_context_ref,
                      plan_revision_id, task_id, outcome_ref, evidence_ref,
                      binding_record_ref, execution_state, evidence_kind,
                      data_contract_state, maximum_claim_strength,
                      result_membership_digest,
                      completeness_membership_digest, content_digest, payload
                    ) VALUES (
                      %(entry_ref)s, %(run_attempt_id)s,
                      %(authority_context_ref)s, %(plan_revision_id)s,
                      %(task_id)s, %(outcome_ref)s, %(evidence_ref)s,
                      %(binding_record_ref)s, %(execution_state)s,
                      %(evidence_kind)s, %(data_contract_state)s,
                      %(maximum_claim_strength)s,
                      %(result_membership_digest)s,
                      %(completeness_membership_digest)s,
                      %(content_digest)s, %(payload)s::jsonb
                    )
                    ON CONFLICT (entry_ref) DO UPDATE
                    SET entry_ref = current.entry_ref
                    WHERE current.run_attempt_id = EXCLUDED.run_attempt_id
                      AND current.authority_context_ref = EXCLUDED.authority_context_ref
                      AND current.plan_revision_id = EXCLUDED.plan_revision_id
                      AND current.task_id = EXCLUDED.task_id
                      AND current.outcome_ref = EXCLUDED.outcome_ref
                      AND current.evidence_ref = EXCLUDED.evidence_ref
                      AND current.binding_record_ref IS NOT DISTINCT FROM EXCLUDED.binding_record_ref
                      AND current.execution_state = EXCLUDED.execution_state
                      AND current.evidence_kind = EXCLUDED.evidence_kind
                      AND current.data_contract_state = EXCLUDED.data_contract_state
                      AND current.maximum_claim_strength = EXCLUDED.maximum_claim_strength
                      AND current.result_membership_digest = EXCLUDED.result_membership_digest
                      AND current.completeness_membership_digest = EXCLUDED.completeness_membership_digest
                      AND current.content_digest = EXCLUDED.content_digest
                      AND current.payload = EXCLUDED.payload
                    RETURNING entry_ref
                    """,
                    {**payload, "payload": _json(payload)},
                    collision="capability_evidence_ledger_entry",
                )
            self.connection.commit()
            return requested
        except Exception:
            self.connection.rollback()
            raise

    def _execution_stage_attempt_closure(
        self,
        *,
        run_attempt_id: str,
        intent_revision_id: str,
        plan_revision_id: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        rows = self._fetchall(
            """
            SELECT acceptance.accepted_attempt_ref, attempt.call_kind
            FROM waje_runtime.durable_call_acceptances acceptance
            JOIN waje_runtime.durable_call_attempts attempt
              ON attempt.run_attempt_id = acceptance.run_attempt_id
             AND attempt.attempt_ref = acceptance.accepted_attempt_ref
            WHERE acceptance.run_attempt_id = %(run_attempt_id)s
              AND attempt.intent_revision_id = %(intent_revision_id)s
              AND attempt.plan_revision_id = %(plan_revision_id)s
              AND attempt.stage_name = 'execute_capability_dag'
            ORDER BY acceptance.accepted_attempt_ref
            """,
            {
                "run_attempt_id": run_attempt_id,
                "intent_revision_id": intent_revision_id,
                "plan_revision_id": plan_revision_id,
            },
        )
        if any(
            str(_field(row, "call_kind", 1) or "") not in {"query", "capability"}
            for row in rows
        ):
            raise EvidenceIntegrityError("capability_execution_stage_call_kind_invalid")
        stage_attempt_refs = tuple(
            str(_field(row, "accepted_attempt_ref", 0) or "") for row in rows
        )
        capability_attempt_refs = tuple(
            str(_field(row, "accepted_attempt_ref", 0) or "")
            for row in rows
            if str(_field(row, "call_kind", 1) or "") == "capability"
        )
        return stage_attempt_refs, capability_attempt_refs

    def load_execution_snapshot(
        self,
        plan_revision_id: str,
    ):
        from bi_agent.runtime.capability_authority import ExecutionSnapshot
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        row = self._fetchone(
            """
            SELECT payload
            FROM waje_runtime.capability_execution_snapshots
            WHERE plan_revision_id = %(plan_revision_id)s
            """,
            {"plan_revision_id": plan_revision_id},
        )
        if row is None:
            return None
        try:
            snapshot = ExecutionSnapshot.from_dict(
                _json_value(_field(row, "payload", 0)) or {}
            )
        except (TypeError, ValueError) as exc:
            raise EvidenceIntegrityError(
                "capability_execution_snapshot_invalid"
            ) from exc
        if snapshot.plan_revision_id != plan_revision_id:
            raise EvidenceIntegrityError("capability_execution_snapshot_plan_mismatch")
        settlement_rows = self._fetchall(
            """
            SELECT input_digest
            FROM waje_runtime.workflow_transition_attempts
            WHERE run_attempt_id = %(run_attempt_id)s
              AND node_name = 'execute_capability_dag'
              AND acceptance_state = 'accepted'
              AND output_payload #>> '{execution_snapshot,execution_snapshot_ref}'
                  = %(execution_snapshot_ref)s
            """,
            {
                "run_attempt_id": snapshot.run_attempt_id,
                "execution_snapshot_ref": snapshot.execution_snapshot_ref,
            },
        )
        if len(settlement_rows) != 1:
            raise EvidenceIntegrityError(
                "capability_execution_snapshot_settlement_missing_or_ambiguous"
            )
        accepted = self.load_accepted_transition(
            run_attempt_id=snapshot.run_attempt_id,
            node_name="execute_capability_dag",
            input_digest=str(_field(settlement_rows[0], "input_digest", 0) or ""),
        )
        if not isinstance(accepted, Mapping) or canonical_value(
            (accepted.get("output_payload") or {}).get("execution_snapshot")
        ) != canonical_value(snapshot.to_dict()):
            raise EvidenceIntegrityError(
                "capability_execution_snapshot_settlement_mismatch"
            )
        transition = accepted.get("transition")
        if not isinstance(transition, DurableTransition):
            raise EvidenceIntegrityError(
                "capability_execution_snapshot_settlement_mismatch"
            )
        outcome_attempt_rows = self._fetchall(
            """
            SELECT attempt_id
            FROM waje_runtime.capability_outcomes
            WHERE plan_revision_id = %(plan_revision_id)s
            ORDER BY attempt_id
            """,
            {"plan_revision_id": snapshot.plan_revision_id},
        )
        outcome_attempt_refs = tuple(
            str(_field(row, "attempt_id", 0) or "") for row in outcome_attempt_rows
        )
        stage_attempt_refs, capability_attempt_refs = (
            self._execution_stage_attempt_closure(
                run_attempt_id=snapshot.run_attempt_id,
                intent_revision_id=transition.intent_revision_id,
                plan_revision_id=snapshot.plan_revision_id,
            )
        )
        if outcome_attempt_refs != capability_attempt_refs:
            raise EvidenceIntegrityError(
                "capability_execution_snapshot_outcome_attempt_closure_invalid"
            )
        if (
            self.attempt_journal.load_stage_attempt_refs(
                run_attempt_id=snapshot.run_attempt_id,
                transition_attempt_id=transition.attempt_id,
                stage_name=transition.node_name,
            )
            != stage_attempt_refs
        ):
            raise EvidenceIntegrityError(
                "capability_execution_snapshot_attempt_seal_mismatch"
            )
        return snapshot

    def load_exploration_stop_record(self, stop_ref: str):
        from bi_agent.runtime.capability_authority import ExplorationStopRecord
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        row = self._fetchone(
            """
            SELECT payload
            FROM waje_runtime.exploration_stop_records
            WHERE stop_ref = %(stop_ref)s
            """,
            {"stop_ref": stop_ref},
        )
        if row is None:
            return None
        try:
            record = ExplorationStopRecord.from_dict(
                _json_value(_field(row, "payload", 0)) or {}
            )
        except (TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("exploration_stop_record_invalid") from exc
        if record.stop_ref != stop_ref:
            raise EvidenceIntegrityError("exploration_stop_record_ref_mismatch")
        return record

    def _validate_accepted_capability_call(
        self,
        attempt: Any,
        outcome: Any,
        evidence_entries: Sequence[Any],
        failures: Sequence[Any],
    ) -> Any:
        from bi_agent.runtime.capability_authority import (
            CapabilityAdapterOutput,
            CapabilityEvidence,
            FailureRecord,
        )
        from bi_agent.runtime.durable_call_journal import (
            DurableCallAcceptance,
            DurableCallAttempt,
        )
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
        )

        row = self._fetchone(
            """
            SELECT call_attempt.payload AS call_attempt_payload,
                   acceptance.payload AS acceptance_payload
            FROM waje_runtime.durable_call_attempts call_attempt
            JOIN waje_runtime.durable_call_acceptances acceptance
              ON acceptance.run_attempt_id = call_attempt.run_attempt_id
             AND acceptance.accepted_attempt_ref = call_attempt.attempt_ref
            WHERE call_attempt.run_attempt_id = %(run_attempt_id)s
              AND call_attempt.attempt_ref = %(attempt_ref)s
            """,
            {
                "run_attempt_id": attempt.run_attempt_id,
                "attempt_ref": attempt.attempt_id,
            },
        )
        if row is None:
            raise EvidenceIntegrityError("capability_call_acceptance_missing")
        try:
            call_attempt = DurableCallAttempt.from_dict(
                _json_value(_field(row, "call_attempt_payload", 0)) or {}
            )
            acceptance = DurableCallAcceptance.from_dict(
                _json_value(_field(row, "acceptance_payload", 1)) or {}
            )
            adapter_output = CapabilityAdapterOutput.from_dict(
                acceptance.output_payload
            )
        except (TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("capability_call_acceptance_invalid") from exc
        spec = call_attempt.spec
        expected_limitation_refs = adapter_output.limitation_refs
        try:
            persisted_evidence_by_ref = {
                entry.evidence_ref: CapabilityEvidence.create(
                    evidence_ref=entry.evidence_ref,
                    binding_record_ref=entry.binding_record_ref,
                    execution_state=entry.execution_state,
                    evidence_kind=entry.evidence_kind,
                    data_contract_state=entry.data_contract_state,
                    supported_claim_kinds=entry.supported_claim_kinds,
                    evidence_strength=entry.evidence_strength,
                    maximum_claim_strength=entry.maximum_claim_strength,
                    observation_facts=entry.observation_facts,
                    scope=entry.scope,
                    window_refs=entry.window_refs,
                    dimension_path=entry.dimension_path,
                    limitation_refs=entry.limitation_refs,
                    result_refs=entry.result_refs,
                    completeness_report_refs=entry.completeness_report_refs,
                    hierarchy_qualified=entry.hierarchy_qualified,
                )
                for entry in evidence_entries
            }
            accepted_evidence_by_ref = {
                evidence.evidence_ref: evidence for evidence in adapter_output.evidence
            }
            expected_failures = (
                ()
                if adapter_output.failure is None
                else (FailureRecord.create(attempt, adapter_output.failure),)
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("capability_call_acceptance_invalid") from exc
        if (
            acceptance.accepted_attempt_ref != call_attempt.attempt_ref
            or acceptance.run_attempt_id != attempt.run_attempt_id
            or acceptance.idempotency_key != spec.idempotency_key
            or acceptance.output_digest != canonical_digest(adapter_output.to_dict())
            or spec.call_kind != "capability"
            or spec.run_attempt_id != attempt.run_attempt_id
            or spec.intent_revision_id != attempt.intent_revision_id
            or spec.plan_revision_id != attempt.plan_revision_id
            or spec.task_id != attempt.task_id
            or spec.input_digest != attempt.input_digest
            or call_attempt.attempt_number != attempt.execution_attempt
            or adapter_output.output_digest != outcome.output_digest
            or adapter_output.status != outcome.status
            or tuple(item.evidence_ref for item in adapter_output.evidence)
            != outcome.evidence_refs
            or adapter_output.affected_obligation_ids != outcome.affected_obligation_ids
            or expected_limitation_refs != outcome.limitation_refs
            or adapter_output.retryability != outcome.retryability
            or (adapter_output.failure is None) != (outcome.failure_ref is None)
            or len(persisted_evidence_by_ref) != len(tuple(evidence_entries))
            or persisted_evidence_by_ref != accepted_evidence_by_ref
            or tuple(failures) != expected_failures
        ):
            raise EvidenceIntegrityError("capability_call_acceptance_invalid")
        return adapter_output

    def accept_execution_settlement(
        self,
        snapshot: Any,
        stop_record: Any,
        transition: DurableTransition,
        input_payload: Mapping[str, Any],
        output_payload: Mapping[str, Any],
        accepted_attempt_refs: Sequence[str],
    ):
        from bi_agent.runtime.capability_authority import (
            ExecutionSnapshot,
            ExplorationStopRecord,
        )
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
            canonical_value,
        )
        from bi_agent.runtime.capability_scheduler import (
            capability_execution_transition_payloads,
        )

        try:
            rebuilt_snapshot = ExecutionSnapshot.from_dict(snapshot.to_dict())
            rebuilt_stop = ExplorationStopRecord.from_dict(stop_record.to_dict())
            rebuilt_transition = DurableTransition.from_dict(transition.to_dict())
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvidenceIntegrityError(
                "capability_execution_snapshot_invalid"
            ) from exc
        if (
            rebuilt_snapshot != snapshot
            or rebuilt_stop != stop_record
            or rebuilt_transition != transition
            or snapshot.run_attempt_id != stop_record.run_attempt_id
            or snapshot.plan_revision_id != stop_record.plan_revision_id
            or snapshot.stop_ref != stop_record.stop_ref
            or snapshot.outcome_refs != stop_record.evaluated_outcome_refs
        ):
            raise EvidenceIntegrityError("capability_execution_snapshot_invalid")
        active_plan = self.resolve_active_plan_revision(snapshot.run_attempt_id)
        if active_plan is None:
            raise EvidenceIntegrityError("capability_execution_plan_not_active")
        expected_input, expected_output = capability_execution_transition_payloads(
            active_plan,
            snapshot,
            stop_record,
        )
        if (
            canonical_value(input_payload) != canonical_value(expected_input)
            or canonical_value(output_payload) != canonical_value(expected_output)
            or transition.node_name != "execute_capability_dag"
            or transition.run_attempt_id != snapshot.run_attempt_id
            or transition.intent_revision_id != active_plan.intent_revision_id
            or transition.input_digest != canonical_digest(input_payload)
            or transition.output_digest != canonical_digest(output_payload)
            or transition.execution_attempt != 1
            or transition.provider_ref != "waje-capability-runtime"
            or transition.model_ref != "deterministic-capability-dag.v1"
            or transition.status != "succeeded"
            or transition.acceptance_state != "accepted"
            or transition.next_transition != "phase03_evidence_bound"
        ):
            raise EvidenceIntegrityError("capability_execution_transition_invalid")
        if isinstance(accepted_attempt_refs, (str, bytes)):
            raise EvidenceIntegrityError("capability_execution_attempt_refs_invalid")
        normalized_attempt_refs = tuple(sorted(set(accepted_attempt_refs)))
        if len(normalized_attempt_refs) != len(tuple(accepted_attempt_refs)):
            raise EvidenceIntegrityError("capability_execution_attempt_refs_invalid")
        try:
            self._lock_single_authority_run(snapshot.run_attempt_id)
            active_plan = self.resolve_active_plan_revision(snapshot.run_attempt_id)
            if (
                active_plan is None
                or active_plan.plan_revision_id != snapshot.plan_revision_id
                or active_plan.authority_context_ref != snapshot.authority_context_ref
            ):
                raise EvidenceIntegrityError("capability_execution_plan_not_active")
            ledger = self.load_decision_ledger(active_plan.intent_revision_id)
            if (
                transition.decision_ledger_position != ledger.position
                or tuple(record.decision_id for record in ledger.active_records())
                != active_plan.decision_refs
            ):
                raise EvidenceIntegrityError(
                    "capability_execution_decision_ledger_mismatch"
                )
            current_head_row = self._fetchone(
                """
                SELECT transition_id
                FROM waje_runtime.workflow_transition_attempts
                WHERE run_attempt_id = %(run_attempt_id)s
                  AND acceptance_state = 'accepted'
                ORDER BY created_at DESC, attempt_id DESC
                LIMIT 1
                FOR UPDATE
                """,
                {"run_attempt_id": snapshot.run_attempt_id},
            )
            current_head_id = (
                str(_field(current_head_row, "transition_id", 0) or "")
                if current_head_row is not None
                else ""
            )
            if current_head_id == transition.transition_id:
                existing_transition = self.load_accepted_transition(
                    run_attempt_id=snapshot.run_attempt_id,
                    node_name="execute_capability_dag",
                    input_digest=transition.input_digest,
                )
                existing = self.load_execution_snapshot(snapshot.plan_revision_id)
                if (
                    existing != snapshot
                    or not isinstance(existing_transition, Mapping)
                    or existing_transition.get("transition") != transition
                    or canonical_value(existing_transition.get("input_payload") or {})
                    != canonical_value(input_payload)
                    or canonical_value(existing_transition.get("output_payload") or {})
                    != canonical_value(output_payload)
                ):
                    raise EvidenceIntegrityError(
                        "capability_execution_settlement_conflict"
                    )
                bound_attempt_refs = self.attempt_journal.load_stage_attempt_refs(
                    run_attempt_id=snapshot.run_attempt_id,
                    transition_attempt_id=transition.attempt_id,
                    stage_name=transition.node_name,
                )
                if bound_attempt_refs != normalized_attempt_refs:
                    raise EvidenceIntegrityError(
                        "capability_execution_attempt_refs_conflict"
                    )
                lifecycle = self._latest_lifecycle_state_locked(snapshot.run_attempt_id)
                if (
                    lifecycle is None
                    or lifecycle.execution_state != "complete"
                    or lifecycle.interaction_state != "active"
                    or lifecycle.evidence_state != "partial"
                    or lifecycle.publication_state != "not_ready"
                    or lifecycle.delivery_state != "pending"
                    or lifecycle.retry_state != "idle"
                    or lifecycle.cancellation_state != "active"
                    or lifecycle.supersession_state != "active"
                ):
                    raise EvidenceIntegrityError(
                        "capability_execution_lifecycle_replay_conflict"
                    )
                execution_result = self._build_authoritative_execution_result(
                    plan_revision=active_plan,
                    snapshot=snapshot,
                    stop_record=stop_record,
                    transition=transition,
                )
                self._persist_execution_result_refs_locked(
                    run_attempt_id=snapshot.run_attempt_id,
                    plan_result_refs=self._plan_result_refs_from_transition_locked(
                        plan_revision=active_plan,
                        accepted_transition_id=(transition.parent_transition_id or ""),
                    ),
                    execution_result_refs=_execution_result_refs(execution_result),
                    transition_replayed=True,
                )
                self.connection.rollback()
                return existing
            if transition.parent_transition_id != (current_head_id or None):
                raise EvidenceIntegrityError(
                    "capability_execution_transition_parent_not_current_head"
                )
            parent = self._fetchone(
                """
                SELECT node_name, intent_revision_id,
                       decision_ledger_position, next_transition,
                       output_payload
                FROM waje_runtime.workflow_transition_attempts
                WHERE transition_id = %(transition_id)s
                  AND run_attempt_id = %(run_attempt_id)s
                  AND acceptance_state = 'accepted'
                """,
                {
                    "transition_id": transition.parent_transition_id,
                    "run_attempt_id": snapshot.run_attempt_id,
                },
            )
            parent_output = (
                _json_value(_field(parent, "output_payload", 4)) or {}
                if parent is not None
                else {}
            )
            if (
                parent is None
                or str(_field(parent, "node_name", 0) or "")
                != (
                    "compile_plan_patch"
                    if active_plan.supersedes_plan_revision_id is not None
                    else "compile_authoritative_plan"
                )
                or str(_field(parent, "intent_revision_id", 1) or "")
                != active_plan.intent_revision_id
                or int(_field(parent, "decision_ledger_position", 2) or 0)
                != ledger.position
                or str(_field(parent, "next_transition", 3) or "")
                != (
                    "phase03_plan_patch_bound"
                    if active_plan.supersedes_plan_revision_id is not None
                    else "phase02_plan_bound"
                )
                or not isinstance(parent_output, Mapping)
                or (parent_output.get("plan_revision") or {}).get("plan_revision_id")
                != active_plan.plan_revision_id
            ):
                raise EvidenceIntegrityError(
                    "capability_execution_transition_parent_invalid"
                )
            lifecycle = self._latest_lifecycle_state_locked(snapshot.run_attempt_id)
            if lifecycle is None:
                raise EvidenceIntegrityError("capability_execution_lifecycle_missing")
            if (
                lifecycle.cancellation_state != "active"
                or lifecycle.supersession_state != "active"
            ):
                raise EvidenceIntegrityError(
                    "capability_execution_lifecycle_not_active"
                )
            if (
                lifecycle.execution_state not in {"running", "waiting"}
                or lifecycle.interaction_state != "active"
                or lifecycle.evidence_state != "not_started"
                or lifecycle.publication_state != "not_ready"
                or lifecycle.delivery_state != "pending"
                or lifecycle.retry_state != "idle"
            ):
                raise EvidenceIntegrityError("capability_execution_lifecycle_not_ready")
            settled_lifecycle = lifecycle.transition(
                execution_state="complete",
                evidence_state="partial",
            )
            existing = self.load_execution_snapshot(snapshot.plan_revision_id)
            if existing is not None:
                raise EvidenceIntegrityError(
                    "capability_execution_snapshot_without_settlement"
                )
            outcome_rows = self._fetchall(
                """
                SELECT outcome_ref, failure_ref, attempt_id
                FROM waje_runtime.capability_outcomes
                WHERE plan_revision_id = %(plan_revision_id)s
                """,
                {"plan_revision_id": snapshot.plan_revision_id},
            )
            if {
                str(_field(item, "outcome_ref", 0) or "") for item in outcome_rows
            } != set(snapshot.outcome_refs):
                raise EvidenceIntegrityError(
                    "capability_execution_outcome_closure_invalid"
                )
            stage_attempt_refs, capability_attempt_refs = (
                self._execution_stage_attempt_closure(
                    run_attempt_id=snapshot.run_attempt_id,
                    intent_revision_id=active_plan.intent_revision_id,
                    plan_revision_id=active_plan.plan_revision_id,
                )
            )
            outcome_attempt_refs = tuple(
                sorted(
                    str(_field(item, "attempt_id", 2) or "") for item in outcome_rows
                )
            )
            if outcome_attempt_refs != capability_attempt_refs:
                raise EvidenceIntegrityError(
                    "capability_execution_outcome_attempt_closure_invalid"
                )
            if stage_attempt_refs != normalized_attempt_refs:
                raise EvidenceIntegrityError(
                    "capability_execution_stage_attempt_closure_invalid"
                )
            if {
                str(_field(item, "failure_ref", 1) or "")
                for item in outcome_rows
                if str(_field(item, "failure_ref", 1) or "")
            } != set(snapshot.failure_refs):
                raise EvidenceIntegrityError(
                    "capability_execution_failure_closure_invalid"
                )
            evidence_rows = self._fetchall(
                """
                SELECT entry_ref
                FROM waje_runtime.capability_evidence_ledger_entries
                WHERE plan_revision_id = %(plan_revision_id)s
                """,
                {"plan_revision_id": snapshot.plan_revision_id},
            )
            if {
                str(_field(item, "entry_ref", 0) or "") for item in evidence_rows
            } != set(snapshot.evidence_entry_refs):
                raise EvidenceIntegrityError(
                    "capability_execution_evidence_closure_invalid"
                )
            execution_result = self._build_authoritative_execution_result(
                plan_revision=active_plan,
                snapshot=snapshot,
                stop_record=stop_record,
                transition=transition,
            )
            stop_payload = canonical_value(stop_record.to_dict())
            self._insert_immutable(
                """
                INSERT INTO waje_runtime.exploration_stop_records AS current(
                  stop_ref, run_attempt_id, plan_revision_id,
                  evaluated_outcome_set_digest, budget_policy_ref, reason,
                  used_budget_units, hard_budget_limit, policy_decision,
                  content_digest, payload
                ) VALUES (
                  %(stop_ref)s, %(run_attempt_id)s, %(plan_revision_id)s,
                  %(evaluated_outcome_set_digest)s, %(budget_policy_ref)s,
                  %(reason)s, %(used_budget_units)s, %(hard_budget_limit)s,
                  %(policy_decision)s::jsonb, %(content_digest)s,
                  %(payload)s::jsonb
                )
                ON CONFLICT (stop_ref) DO UPDATE
                SET stop_ref = current.stop_ref
                WHERE current.run_attempt_id = EXCLUDED.run_attempt_id
                  AND current.plan_revision_id = EXCLUDED.plan_revision_id
                  AND current.evaluated_outcome_set_digest = EXCLUDED.evaluated_outcome_set_digest
                  AND current.budget_policy_ref = EXCLUDED.budget_policy_ref
                  AND current.reason = EXCLUDED.reason
                  AND current.used_budget_units = EXCLUDED.used_budget_units
                  AND current.hard_budget_limit IS NOT DISTINCT FROM EXCLUDED.hard_budget_limit
                  AND current.policy_decision = EXCLUDED.policy_decision
                  AND current.content_digest = EXCLUDED.content_digest
                  AND current.payload = EXCLUDED.payload
                RETURNING stop_ref
                """,
                {
                    **stop_payload,
                    "policy_decision": _json(stop_payload["policy_decision"]),
                    "payload": _json(stop_payload),
                },
                collision="exploration_stop_record",
            )
            snapshot_payload = canonical_value(snapshot.to_dict())
            self._insert_immutable(
                """
                INSERT INTO waje_runtime.capability_execution_snapshots AS current(
                  execution_snapshot_ref, run_attempt_id,
                  authority_context_ref, plan_revision_id, stop_ref,
                  outcome_set_digest, evidence_ledger_digest,
                  content_digest, payload
                ) VALUES (
                  %(execution_snapshot_ref)s, %(run_attempt_id)s,
                  %(authority_context_ref)s, %(plan_revision_id)s,
                  %(stop_ref)s, %(outcome_set_digest)s,
                  %(evidence_ledger_digest)s, %(content_digest)s,
                  %(payload)s::jsonb
                )
                ON CONFLICT (execution_snapshot_ref) DO UPDATE
                SET execution_snapshot_ref = current.execution_snapshot_ref
                WHERE current.run_attempt_id = EXCLUDED.run_attempt_id
                  AND current.authority_context_ref = EXCLUDED.authority_context_ref
                  AND current.plan_revision_id = EXCLUDED.plan_revision_id
                  AND current.stop_ref = EXCLUDED.stop_ref
                  AND current.outcome_set_digest = EXCLUDED.outcome_set_digest
                  AND current.evidence_ledger_digest = EXCLUDED.evidence_ledger_digest
                  AND current.content_digest = EXCLUDED.content_digest
                  AND current.payload = EXCLUDED.payload
                RETURNING execution_snapshot_ref
                """,
                {**snapshot_payload, "payload": _json(snapshot_payload)},
                collision="capability_execution_snapshot",
            )
            self._save_transition_attempt_locked(
                transition=transition,
                input_payload=input_payload,
                output_payload=output_payload,
            )
            self.attempt_journal.bind_stage(
                run_attempt_id=snapshot.run_attempt_id,
                transition_attempt_id=transition.attempt_id,
                stage_name=transition.node_name,
                attempt_refs=normalized_attempt_refs,
                commit=False,
            )
            self._persist_execution_result_refs_locked(
                run_attempt_id=snapshot.run_attempt_id,
                plan_result_refs=self._plan_result_refs_from_transition_locked(
                    plan_revision=active_plan,
                    accepted_transition_id=(transition.parent_transition_id or ""),
                ),
                execution_result_refs=_execution_result_refs(execution_result),
                transition_replayed=False,
            )
            self._append_lifecycle_state_locked(settled_lifecycle)
            self.connection.commit()
            return snapshot
        except Exception:
            self.connection.rollback()
            raise

    def save_claim_coverage_transition(
        self,
        *,
        plan_revision: PlanRevision,
        execution_result: Any,
        checkpoint: Any,
        input_payload: Mapping[str, Any],
        output_payload: Mapping[str, Any],
        accepted_attempt_refs: Sequence[str],
    ) -> Mapping[str, Any]:
        from bi_agent.runtime.authoritative_execution_result import (
            validate_typed_authoritative_execution_result,
        )
        from bi_agent.runtime.claim_coverage import (
            ClaimCoverageCheckpoint,
            claim_coverage_transition_payloads,
        )
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
            canonical_value,
        )

        if type(plan_revision) is not PlanRevision:
            raise EvidenceIntegrityError("claim_coverage_plan_invalid")
        try:
            plan = PlanRevision.from_dict(plan_revision.to_dict())
            execution = validate_typed_authoritative_execution_result(execution_result)
            if type(checkpoint) is not ClaimCoverageCheckpoint:
                raise TypeError("checkpoint")
            replayed_checkpoint = ClaimCoverageCheckpoint.create(
                plan_revision=plan,
                execution_result=execution,
                evaluation=checkpoint.evaluation,
                decision=checkpoint.decision,
                plan_patch=checkpoint.plan_patch,
                transition=checkpoint.transition,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("claim_coverage_checkpoint_invalid") from exc
        if (
            plan != plan_revision
            or replayed_checkpoint != checkpoint
            or execution.plan_revision != plan
        ):
            raise EvidenceIntegrityError("claim_coverage_checkpoint_invalid")
        expected_input, expected_output = claim_coverage_transition_payloads(
            evaluation=checkpoint.evaluation,
            decision=checkpoint.decision,
            plan_patch=checkpoint.plan_patch,
        )
        transition = checkpoint.transition
        if (
            canonical_value(input_payload) != canonical_value(expected_input)
            or canonical_value(output_payload) != canonical_value(expected_output)
            or transition.input_digest != canonical_digest(input_payload)
            or transition.output_digest != canonical_digest(output_payload)
        ):
            raise EvidenceIntegrityError("claim_coverage_transition_payload_invalid")
        if isinstance(accepted_attempt_refs, (str, bytes)):
            raise EvidenceIntegrityError("claim_coverage_attempt_refs_invalid")
        normalized_attempt_refs = tuple(accepted_attempt_refs)
        expected_attempt_count = (
            1 if checkpoint.decision.decision_authority == "provider" else 0
        )
        if len(normalized_attempt_refs) != expected_attempt_count or len(
            set(normalized_attempt_refs)
        ) != len(normalized_attempt_refs):
            raise EvidenceIntegrityError("claim_coverage_attempt_refs_invalid")
        coverage_refs = canonical_value(
            {
                "schema_version": checkpoint.schema_version,
                "source_plan_revision_id": plan.plan_revision_id,
                "source_execution_result_ref": (
                    execution.authoritative_execution_result_ref
                ),
                "claim_coverage_checkpoint_ref": (checkpoint.checkpoint_ref),
                "claim_coverage_checkpoint_digest": (checkpoint.content_digest),
                "claim_coverage_evaluation_ref": (checkpoint.evaluation_ref),
                "plan_expansion_decision_ref": checkpoint.decision_ref,
                "decision": checkpoint.decision.decision,
                "plan_patch_ref": checkpoint.plan_patch_ref,
                "accepted_transition_id": transition.transition_id,
            }
        )
        try:
            self._lock_single_authority_run(plan.run_attempt_id)
            active_plan = self.resolve_active_plan_revision(plan.run_attempt_id)
            if active_plan != plan:
                raise EvidenceIntegrityError("claim_coverage_plan_not_active")
            snapshot = self.load_execution_snapshot(plan.plan_revision_id)
            stop_record = self.load_exploration_stop_record(execution.stop_ref)
            if snapshot is None or stop_record is None:
                raise EvidenceIntegrityError("claim_coverage_execution_missing")
            persisted_execution = self._build_authoritative_execution_result(
                plan_revision=plan,
                snapshot=snapshot,
                stop_record=stop_record,
                transition=execution.durable_transition,
            )
            if persisted_execution != execution:
                raise EvidenceIntegrityError("claim_coverage_execution_conflict")
            current_head_row = self._fetchone(
                """
                SELECT transition_id
                FROM waje_runtime.workflow_transition_attempts
                WHERE run_attempt_id = %(run_attempt_id)s
                  AND acceptance_state = 'accepted'
                ORDER BY created_at DESC, attempt_id DESC
                LIMIT 1
                FOR UPDATE
                """,
                {"run_attempt_id": plan.run_attempt_id},
            )
            current_head_id = (
                str(_field(current_head_row, "transition_id", 0) or "")
                if current_head_row is not None
                else ""
            )
            lifecycle = self._latest_lifecycle_state_locked(plan.run_attempt_id)
            request = self._single_authority_request_locked(plan.run_attempt_id)
            plan_refs = request.get("plan_result_refs")
            execution_refs = request.get("execution_result_refs")
            if (
                not isinstance(plan_refs, Mapping)
                or plan_refs.get("plan_revision_id") != plan.plan_revision_id
                or not isinstance(execution_refs, Mapping)
                or execution_refs.get("authoritative_execution_result_ref")
                != execution.authoritative_execution_result_ref
                or lifecycle is None
                or lifecycle.interaction_state != "active"
                or lifecycle.publication_state != "not_ready"
                or lifecycle.delivery_state != "pending"
                or lifecycle.retry_state != "idle"
                or lifecycle.cancellation_state != "active"
                or lifecycle.supersession_state != "active"
            ):
                raise EvidenceIntegrityError("claim_coverage_active_chain_invalid")
            if current_head_id == transition.transition_id:
                existing = self.load_accepted_transition(
                    run_attempt_id=plan.run_attempt_id,
                    node_name="evaluate_claim_coverage",
                    input_digest=transition.input_digest,
                )
                bound_attempt_refs = self.attempt_journal.load_stage_attempt_refs(
                    run_attempt_id=plan.run_attempt_id,
                    transition_attempt_id=transition.attempt_id,
                    stage_name="evaluate_claim_coverage",
                )
                expected_execution_state = (
                    "waiting" if checkpoint.decision.decision == "patch" else "complete"
                )
                expected_evidence_state = (
                    "not_started"
                    if checkpoint.decision.decision == "patch"
                    else "partial"
                )
                if (
                    not isinstance(existing, Mapping)
                    or existing.get("transition") != transition
                    or canonical_value(existing.get("input_payload") or {})
                    != canonical_value(input_payload)
                    or canonical_value(existing.get("output_payload") or {})
                    != canonical_value(output_payload)
                    or bound_attempt_refs != normalized_attempt_refs
                    or canonical_value(request.get("claim_coverage_refs"))
                    != coverage_refs
                    or lifecycle.execution_state != expected_execution_state
                    or lifecycle.evidence_state != expected_evidence_state
                ):
                    raise EvidenceIntegrityError("claim_coverage_replay_conflict")
                self.connection.rollback()
                return {
                    "checkpoint": checkpoint.to_dict(),
                    "transition": transition.to_dict(),
                    "replayed": True,
                }
            if (
                transition.parent_transition_id != current_head_id
                or current_head_id != execution.transition_id
                or lifecycle.execution_state != "complete"
                or lifecycle.evidence_state != "partial"
                or "claim_coverage_refs" in request
            ):
                raise EvidenceIntegrityError("claim_coverage_transition_parent_invalid")
            self._save_transition_attempt_locked(
                transition=transition,
                input_payload=input_payload,
                output_payload=output_payload,
            )
            self.attempt_journal.bind_stage(
                run_attempt_id=plan.run_attempt_id,
                transition_attempt_id=transition.attempt_id,
                stage_name="evaluate_claim_coverage",
                attempt_refs=normalized_attempt_refs,
                commit=False,
            )
            self._replace_single_authority_request_locked(
                plan.run_attempt_id,
                current_request=request,
                next_request={
                    **request,
                    "claim_coverage_refs": coverage_refs,
                },
                conflict_code="claim_coverage_refs_conflict",
            )
            if checkpoint.decision.decision == "patch":
                lifecycle = lifecycle.transition(
                    execution_state="waiting",
                    evidence_state="not_started",
                )
                self._append_lifecycle_state_locked(lifecycle)
            self.connection.commit()
            return {
                "checkpoint": checkpoint.to_dict(),
                "transition": transition.to_dict(),
                "replayed": False,
            }
        except Exception:
            self.connection.rollback()
            raise

    def load_planner_proposal(self, planner_proposal_id: str) -> PlannerProposal | None:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        row = self._fetchone(
            """
            SELECT payload FROM waje_runtime.planner_proposals
            WHERE planner_proposal_id = %(planner_proposal_id)s
            """,
            {"planner_proposal_id": planner_proposal_id},
        )
        if row is None:
            return None
        try:
            proposal = PlannerProposal.from_dict(
                _json_value(_field(row, "payload", 0)) or {}
            )
        except (TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("planner_proposal_invalid") from exc
        if proposal.planner_proposal_id != planner_proposal_id:
            raise EvidenceIntegrityError("planner_proposal_id_mismatch")
        return proposal

    def load_proposal_admission(
        self, proposal_admission_id: str
    ) -> ProposalAdmissionRecord | None:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        row = self._fetchone(
            """
            SELECT payload FROM waje_runtime.proposal_admission_records
            WHERE proposal_admission_id = %(proposal_admission_id)s
            """,
            {"proposal_admission_id": proposal_admission_id},
        )
        if row is None:
            return None
        try:
            admission = ProposalAdmissionRecord.from_dict(
                _json_value(_field(row, "payload", 0)) or {}
            )
        except (TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("proposal_admission_invalid") from exc
        if admission.proposal_admission_id != proposal_admission_id:
            raise EvidenceIntegrityError("proposal_admission_id_mismatch")
        return admission

    def save_decision_options_transition(
        self,
        *,
        intent_revision_id: str,
        options: Sequence[Mapping[str, Any]],
        transition: DurableTransition,
        input_payload: Mapping[str, Any],
        output_payload: Mapping[str, Any],
        accepted_attempt_refs: Sequence[str],
    ) -> str:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
            canonical_value,
        )

        required = {
            "slot_id",
            "option_id",
            "typed_value",
            "display_label",
            "display_description",
            "recommended",
        }
        normalized: list[dict[str, Any]] = []
        for option in options:
            if not isinstance(option, Mapping) or set(option) != required:
                raise EvidenceIntegrityError("decision_option_shape_invalid")
            if (
                not isinstance(option["slot_id"], str)
                or not option["slot_id"].strip()
                or not isinstance(option["option_id"], str)
                or not option["option_id"].strip()
                or not isinstance(option["typed_value"], Mapping)
                or not option["typed_value"]
                or not isinstance(option["display_label"], str)
                or not option["display_label"].strip()
                or not isinstance(option["display_description"], str)
                or not option["display_description"].strip()
                or not isinstance(option["recommended"], bool)
            ):
                raise EvidenceIntegrityError("decision_option_shape_invalid")
            normalized.append(canonical_value(dict(option)))
        normalized.sort(key=lambda item: (item["slot_id"], item["option_id"]))
        if len({(item["slot_id"], item["option_id"]) for item in normalized}) != len(
            normalized
        ):
            raise EvidenceIntegrityError("decision_option_identity_duplicated")
        if (
            len({item["slot_id"] for item in normalized}) != 1
            or sum(item["recommended"] for item in normalized) != 1
        ):
            raise EvidenceIntegrityError("decision_option_set_invalid")
        if (
            isinstance(accepted_attempt_refs, (str, bytes))
            or len(tuple(accepted_attempt_refs)) != 1
        ):
            raise EvidenceIntegrityError(
                "clarification_provider_attempt_cardinality_invalid"
            )
        normalized_attempt_refs = tuple(accepted_attempt_refs)
        option_set_digest = canonical_digest(normalized)
        try:
            self._lock_single_authority_run(transition.run_attempt_id)
            active = self.resolve_active_intent_revision(transition.run_attempt_id)
            if (
                active is None
                or active.intent_revision_id != intent_revision_id
                or transition.intent_revision_id != intent_revision_id
                or transition.acceptance_state != "accepted"
            ):
                raise EvidenceIntegrityError("decision_option_intent_not_active")
            known_slots = {
                str(slot["slot_id"]): slot for slot in active.ambiguity_slots
            }
            for option in normalized:
                slot = known_slots.get(option["slot_id"])
                if slot is None:
                    raise EvidenceIntegrityError("decision_option_slot_unknown")
                try:
                    normalized_value, value_ref = normalize_temporal_decision_value(
                        slot_id=option["slot_id"],
                        value=option["typed_value"],
                        time_spec=active.time_spec,
                    )
                    expected_option_id = temporal_decision_option_id(
                        slot_id=option["slot_id"],
                        value=normalized_value,
                        time_spec=active.time_spec,
                    )
                except (TypeError, ValueError) as exc:
                    raise EvidenceIntegrityError(
                        "decision_option_typed_value_invalid"
                    ) from exc
                if (
                    value_ref not in tuple(slot.get("allowed_value_refs") or ())
                    or canonical_value(option["typed_value"])
                    != canonical_value(normalized_value)
                    or option["option_id"] != expected_option_id
                ):
                    raise EvidenceIntegrityError("decision_option_typed_value_invalid")
                body = canonical_value(
                    {
                        "intent_revision_id": intent_revision_id,
                        **option,
                        "option_set_digest": option_set_digest,
                    }
                )
                content_digest = canonical_digest(body)
                payload = {**body, "content_digest": content_digest}
                self._execute(
                    """
                    INSERT INTO waje_runtime.decision_options(
                      intent_revision_id, slot_id, option_id, typed_value,
                      display_label, display_description, recommended,
                      option_set_digest, content_digest, payload
                    ) VALUES (
                      %(intent_revision_id)s, %(slot_id)s, %(option_id)s,
                      %(typed_value)s::jsonb, %(display_label)s,
                      %(display_description)s, %(recommended)s,
                      %(option_set_digest)s, %(content_digest)s,
                      %(payload)s::jsonb
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    {
                        **body,
                        "typed_value": _json(body["typed_value"]),
                        "content_digest": content_digest,
                        "payload": _json(payload),
                    },
                    commit=False,
                )
                stored = self._fetchone(
                    """
                    SELECT payload
                    FROM waje_runtime.decision_options
                    WHERE intent_revision_id = %(intent_revision_id)s
                      AND slot_id = %(slot_id)s
                      AND option_id = %(option_id)s
                    """,
                    body,
                )
                if (
                    stored is None
                    or canonical_value(_json_value(_field(stored, "payload", 0)) or {})
                    != payload
                ):
                    raise EvidenceIntegrityError("decision_option_immutable_conflict")
            self._save_transition_attempt_locked(
                transition=transition,
                input_payload=input_payload,
                output_payload=output_payload,
            )
            self.attempt_journal.bind_stage(
                run_attempt_id=transition.run_attempt_id,
                transition_attempt_id=transition.attempt_id,
                stage_name="generate_clarification",
                attempt_refs=normalized_attempt_refs,
                commit=False,
            )
            self.connection.commit()
            return option_set_digest
        except Exception:
            self.connection.rollback()
            raise

    def save_waiting_transition(
        self,
        *,
        transition: DurableTransition,
        lifecycle: LifecycleState,
        input_payload: Mapping[str, Any],
        output_payload: Mapping[str, Any],
    ) -> str:
        """Atomically accept the durable pause point and waiting lifecycle."""
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        if (
            transition.node_name != "persist_waiting_for_decision"
            or transition.next_transition != "await_user_decision"
            or transition.acceptance_state != "accepted"
            or not transition.parent_transition_id
            or lifecycle.run_attempt_id != transition.run_attempt_id
            or lifecycle.execution_state != "waiting"
            or lifecycle.interaction_state != "waiting_for_user"
            or lifecycle.cancellation_state != "active"
            or lifecycle.supersession_state != "active"
        ):
            raise EvidenceIntegrityError("waiting_transition_contract_invalid")
        expected_output = canonical_value(
            {
                "status": "waiting_for_clarification",
                "lifecycle_state": lifecycle.to_dict(),
            }
        )
        if canonical_value(output_payload) != expected_output:
            raise EvidenceIntegrityError("waiting_transition_output_invalid")
        try:
            self._lock_single_authority_run(transition.run_attempt_id)
            active = self.resolve_active_intent_revision(transition.run_attempt_id)
            if (
                active is None
                or active.intent_revision_id != transition.intent_revision_id
            ):
                raise EvidenceIntegrityError("waiting_transition_intent_not_active")
            ledger = self.load_decision_ledger(active.intent_revision_id)
            if ledger.position != transition.decision_ledger_position:
                raise EvidenceIntegrityError(
                    "waiting_transition_ledger_position_mismatch"
                )
            parent = self._fetchone(
                """
                SELECT intent_revision_id, decision_ledger_position,
                       next_transition
                FROM waje_runtime.workflow_transition_attempts
                WHERE transition_id = %(parent_transition_id)s
                  AND run_attempt_id = %(run_attempt_id)s
                  AND acceptance_state = 'accepted'
                """,
                {
                    "parent_transition_id": transition.parent_transition_id,
                    "run_attempt_id": transition.run_attempt_id,
                },
            )
            if (
                parent is None
                or str(_field(parent, "intent_revision_id", 0) or "")
                != transition.intent_revision_id
                or int(_field(parent, "decision_ledger_position", 1) or 0)
                != transition.decision_ledger_position
                or str(_field(parent, "next_transition", 2) or "")
                != "persist_waiting_for_decision"
            ):
                raise EvidenceIntegrityError("waiting_transition_parent_invalid")
            current = self._latest_lifecycle_state_locked(transition.run_attempt_id)
            if current is None:
                raise EvidenceIntegrityError("waiting_transition_lifecycle_missing")
            if current.content_digest != lifecycle.content_digest:
                expected_lifecycle = current.transition(
                    execution_state="waiting",
                    interaction_state="waiting_for_user",
                )
                if expected_lifecycle != lifecycle:
                    raise EvidenceIntegrityError(
                        "waiting_transition_lifecycle_conflict"
                    )
            self._append_lifecycle_state_locked(lifecycle)
            transition_status = self._save_transition_attempt_locked(
                transition=transition,
                input_payload=input_payload,
                output_payload=output_payload,
            )
            self.connection.commit()
            return transition_status
        except Exception:
            self.connection.rollback()
            raise

    def append_decision_record_transition(
        self,
        *,
        decision: DecisionRecord,
        transition: DurableTransition,
        input_payload: Mapping[str, Any],
        output_payload: Mapping[str, Any],
        provider_attempt_refs: Sequence[str] | None,
    ) -> tuple[DecisionRecord, int]:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        if provider_attempt_refs is None:
            normalized_attempt_refs = None
        elif (
            isinstance(provider_attempt_refs, (str, bytes))
            or len(tuple(provider_attempt_refs)) != 1
        ):
            raise EvidenceIntegrityError(
                "decision_provider_attempt_cardinality_invalid"
            )
        else:
            normalized_attempt_refs = tuple(provider_attempt_refs)
        try:
            self._lock_single_authority_run(transition.run_attempt_id)
            active = self.resolve_active_intent_revision(transition.run_attempt_id)
            if (
                active is None
                or active.intent_revision_id != decision.intent_revision_id
                or transition.intent_revision_id != decision.intent_revision_id
                or transition.acceptance_state != "accepted"
            ):
                raise EvidenceIntegrityError("decision_intent_not_active")
            ledger = self.load_decision_ledger(decision.intent_revision_id)
            replay_candidate = self._fetchone(
                """
                SELECT ledger_position, payload
                FROM waje_runtime.decision_records
                WHERE decision_id = %(decision_id)s
                   OR (
                     intent_revision_id = %(intent_revision_id)s
                     AND slot_id = %(slot_id)s
                     AND option_id IS NOT DISTINCT FROM %(option_id)s
                     AND content_digest = %(content_digest)s
                   )
                ORDER BY ledger_position
                LIMIT 1
                """,
                decision.to_dict(),
            )
            if replay_candidate is not None:
                replayed_decision = DecisionRecord.from_dict(
                    _json_value(_field(replay_candidate, "payload", 1)) or {}
                )
                if replayed_decision != decision:
                    raise EvidenceIntegrityError("decision_record_immutable_conflict")
                ledger_position = int(
                    _field(replay_candidate, "ledger_position", 0) or 0
                )
            else:
                ledger.append(decision)
                ledger_position = ledger.position + 1
            if transition.decision_ledger_position != ledger_position:
                raise EvidenceIntegrityError("decision_ledger_position_mismatch")
            if decision.option_id:
                option = self._fetchone(
                    """
                    SELECT typed_value
                    FROM waje_runtime.decision_options
                    WHERE intent_revision_id = %(intent_revision_id)s
                      AND slot_id = %(slot_id)s
                      AND option_id = %(option_id)s
                    FOR UPDATE
                    """,
                    decision.to_dict(),
                )
                if option is None or canonical_value(
                    _json_value(_field(option, "typed_value", 0))
                ) != canonical_value(decision.value):
                    raise EvidenceIntegrityError("decision_option_value_mismatch")
            self._execute(
                """
                INSERT INTO waje_runtime.decision_records(
                  ledger_position, decision_id, run_attempt_id,
                  intent_revision_id, slot_id, option_id,
                  source, status, materiality, invalidated_by_revision_id,
                  supersedes_decision_id, content_digest, payload
                ) VALUES (
                  %(ledger_position)s, %(decision_id)s, %(run_attempt_id)s,
                  %(intent_revision_id)s, %(slot_id)s, %(option_id)s,
                  %(source)s, %(status)s, %(materiality)s,
                  %(invalidated_by_revision_id)s, %(supersedes_decision_id)s,
                  %(content_digest)s, %(payload)s::jsonb
                )
                ON CONFLICT DO NOTHING
                """,
                {
                    **decision.to_dict(),
                    "ledger_position": ledger_position,
                    "run_attempt_id": transition.run_attempt_id,
                    "payload": _json(decision.to_dict()),
                },
                commit=False,
            )
            stored = self._fetchone(
                """
                SELECT ledger_position, payload
                FROM waje_runtime.decision_records
                WHERE decision_id = %(decision_id)s
                   OR (
                     intent_revision_id = %(intent_revision_id)s
                     AND slot_id = %(slot_id)s
                     AND option_id IS NOT DISTINCT FROM %(option_id)s
                     AND content_digest = %(content_digest)s
                   )
                ORDER BY ledger_position
                LIMIT 2
                """,
                decision.to_dict(),
            )
            if stored is None:
                raise EvidenceIntegrityError("decision_record_insert_missing")
            stored_decision = DecisionRecord.from_dict(
                _json_value(_field(stored, "payload", 1)) or {}
            )
            if stored_decision != decision:
                raise EvidenceIntegrityError("decision_record_immutable_conflict")
            stored_ledger_position = int(_field(stored, "ledger_position", 0) or 0)
            if stored_ledger_position != ledger_position:
                raise EvidenceIntegrityError("decision_ledger_position_mismatch")
            self._save_transition_attempt_locked(
                transition=transition,
                input_payload=input_payload,
                output_payload=output_payload,
            )
            if normalized_attempt_refs is not None:
                self.attempt_journal.bind_stage(
                    run_attempt_id=transition.run_attempt_id,
                    transition_attempt_id=transition.attempt_id,
                    stage_name="bind_free_text_submission",
                    attempt_refs=normalized_attempt_refs,
                    commit=False,
                )
            self.connection.commit()
            return stored_decision, stored_ledger_position
        except Exception:
            self.connection.rollback()
            raise

    def load_decision_ledger(self, intent_revision_id: str) -> DecisionLedger:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        rows = self._fetchall(
            """
            WITH RECURSIVE revision_chain AS (
              SELECT intent_revision_id, supersedes_intent_revision_id
              FROM waje_runtime.intent_revisions
              WHERE intent_revision_id = %(intent_revision_id)s
              UNION ALL
              SELECT parent.intent_revision_id,
                     parent.supersedes_intent_revision_id
              FROM waje_runtime.intent_revisions parent
              JOIN revision_chain child
                ON parent.intent_revision_id = child.supersedes_intent_revision_id
            )
            SELECT record.payload
            FROM revision_chain
            JOIN waje_runtime.decision_records record
              USING (intent_revision_id)
            ORDER BY record.ledger_position
            """,
            {"intent_revision_id": intent_revision_id},
        )
        ledger = DecisionLedger()
        try:
            for row in rows:
                ledger = ledger.append(
                    DecisionRecord.from_dict(
                        _json_value(_field(row, "payload", 0)) or {}
                    )
                )
        except (TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("decision_ledger_invalid") from exc
        return ledger

    def accept_decision_option(
        self,
        *,
        run_attempt_id: str,
        option_id: str,
        source: str = "user",
    ) -> dict[str, Any]:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
        )

        active = self.resolve_active_intent_revision(run_attempt_id)
        if active is None:
            raise EvidenceIntegrityError("decision_intent_not_active")
        option = self._fetchone(
            """
            SELECT slot_id, typed_value
            FROM waje_runtime.decision_options
            WHERE intent_revision_id = %(intent_revision_id)s
              AND option_id = %(option_id)s
            """,
            {
                "intent_revision_id": active.intent_revision_id,
                "option_id": option_id,
            },
        )
        if option is None:
            raise EvidenceIntegrityError("decision_option_unknown")
        slot_id = str(_field(option, "slot_id", 0) or "")
        typed_value = _json_value(_field(option, "typed_value", 1))
        slot = next(
            (item for item in active.ambiguity_slots if item.get("slot_id") == slot_id),
            None,
        )
        if slot is None:
            raise EvidenceIntegrityError("decision_option_slot_unknown")
        affected_by_kind = {
            "baseline": ("baseline_refs", "resolved_window_refs"),
            "comparison_window": ("baseline_refs", "resolved_window_refs"),
            "time": ("time_spec", "resolved_window_refs"),
            "scope": ("scope", "filters"),
            "metric": ("target_metric_refs", "analysis_axes"),
            "goal": ("goal_bindings", "desired_decisions"),
        }
        affected_plan_fields = affected_by_kind.get(
            str(slot.get("slot_kind") or ""),
            (f"ambiguity_slot:{slot_id}",),
        )
        decision = DecisionRecord.create(
            intent_revision_id=active.intent_revision_id,
            slot_id=slot_id,
            value=typed_value,
            source=source,
            status="user_confirmed",
            materiality=str(slot.get("materiality") or "material"),
            affected_plan_fields=affected_plan_fields,
            option_id=option_id,
        )
        ledger = self.load_decision_ledger(active.intent_revision_id)
        existing = next(
            (
                record
                for record in ledger.records
                if record.intent_revision_id == active.intent_revision_id
                and record.slot_id == slot_id
                and record.option_id == option_id
            ),
            None,
        )
        ledger_position = (
            self._decision_ledger_position(existing.decision_id)
            if existing is not None
            else ledger.position + 1
        )
        input_payload = {
            "intent_revision_id": active.intent_revision_id,
            "slot_id": slot_id,
            "selected_option_id": option_id,
            "source": source,
        }
        accepted = self.load_accepted_transition(
            run_attempt_id=run_attempt_id,
            node_name="accept_material_decision",
            input_digest=canonical_digest(input_payload),
        )
        if accepted is not None:
            output_payload = accepted.get("output_payload") or {}
            try:
                replayed = DecisionRecord.from_dict(output_payload["decision"])
            except (KeyError, TypeError, ValueError) as exc:
                raise EvidenceIntegrityError(
                    "accepted_decision_transition_invalid"
                ) from exc
            if replayed != decision:
                raise EvidenceIntegrityError("accepted_decision_transition_conflict")
            return {
                "decision": replayed.to_dict(),
                "decision_ledger_position": int(
                    accepted["transition"].decision_ledger_position
                ),
                "durable_checkpoint": accepted["transition"].to_dict(),
                "replayed": True,
            }
        parent = self._fetchone(
            """
            SELECT transition_id
            FROM waje_runtime.workflow_transition_attempts
            WHERE run_attempt_id = %(run_attempt_id)s
              AND acceptance_state = 'accepted'
            ORDER BY created_at DESC, attempt_id DESC
            LIMIT 1
            """,
            {"run_attempt_id": run_attempt_id},
        )
        output_payload = {"decision": decision.to_dict()}
        transition = DurableTransition.create(
            node_name="accept_material_decision",
            parent_transition_id=(
                str(_field(parent, "transition_id", 0) or "") or None
            ),
            run_attempt_id=run_attempt_id,
            intent_revision_id=active.intent_revision_id,
            decision_ledger_position=ledger_position,
            input_digest=canonical_digest(input_payload),
            output_digest=canonical_digest(output_payload),
            execution_attempt=1,
            provider_ref="user_protocol",
            model_ref="stable_option_contract",
            status="succeeded",
            acceptance_state="accepted",
            next_transition="compile_authoritative_plan",
        )
        stored, stored_position = self.append_decision_record_transition(
            decision=decision,
            transition=transition,
            input_payload=input_payload,
            output_payload=output_payload,
            provider_attempt_refs=None,
        )
        lifecycle = self.latest_lifecycle_state(run_attempt_id)
        if lifecycle is not None and lifecycle.interaction_state == "waiting_for_user":
            self.append_lifecycle_state(
                lifecycle.transition(interaction_state="active")
            )
        return {
            "decision": stored.to_dict(),
            "decision_ledger_position": stored_position,
            "durable_checkpoint": transition.to_dict(),
            "replayed": False,
        }

    def load_decision_options(
        self, intent_revision_id: str
    ) -> tuple[dict[str, Any], ...]:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
            canonical_value,
        )

        rows = self._fetchall(
            """
            SELECT intent_revision_id, slot_id, option_id, typed_value,
                   display_label, display_description, recommended,
                   option_set_digest, content_digest, payload
            FROM waje_runtime.decision_options
            WHERE intent_revision_id = %(intent_revision_id)s
            ORDER BY slot_id, option_id
            """,
            {"intent_revision_id": intent_revision_id},
        )
        options: list[dict[str, Any]] = []
        option_set_digests: set[str] = set()
        for row in rows:
            row_intent_revision_id = _field(row, "intent_revision_id", 0)
            slot_id = _field(row, "slot_id", 1)
            option_id = _field(row, "option_id", 2)
            typed_value = _json_value(_field(row, "typed_value", 3))
            display_label = _field(row, "display_label", 4)
            display_description = _field(row, "display_description", 5)
            recommended = _field(row, "recommended", 6)
            option_set_digest = _field(row, "option_set_digest", 7)
            content_digest = _field(row, "content_digest", 8)
            payload = _json_value(_field(row, "payload", 9))
            if (
                row_intent_revision_id != intent_revision_id
                or not isinstance(slot_id, str)
                or not slot_id
                or not isinstance(option_id, str)
                or not option_id
                or not isinstance(typed_value, Mapping)
                or not typed_value
                or not isinstance(display_label, str)
                or not display_label.strip()
                or not isinstance(display_description, str)
                or not display_description.strip()
                or not isinstance(recommended, bool)
                or not isinstance(option_set_digest, str)
                or len(option_set_digest) != 64
                or not isinstance(content_digest, str)
                or len(content_digest) != 64
            ):
                raise EvidenceIntegrityError("decision_option_record_invalid")
            option = canonical_value(
                {
                    "slot_id": slot_id,
                    "option_id": option_id,
                    "typed_value": typed_value,
                    "display_label": display_label,
                    "display_description": display_description,
                    "recommended": recommended,
                }
            )
            body = canonical_value(
                {
                    "intent_revision_id": intent_revision_id,
                    **option,
                    "option_set_digest": option_set_digest,
                }
            )
            expected_payload = {**body, "content_digest": content_digest}
            if (
                canonical_digest(body) != content_digest
                or canonical_value(payload) != expected_payload
            ):
                raise EvidenceIntegrityError("decision_option_record_invalid")
            options.append(option)
            option_set_digests.add(option_set_digest)
        if options and (
            len(option_set_digests) != 1
            or next(iter(option_set_digests)) != canonical_digest(options)
            or len({item["slot_id"] for item in options}) != 1
            or sum(item["recommended"] for item in options) != 1
        ):
            raise EvidenceIntegrityError("decision_option_set_invalid")
        return tuple(options)

    def record_typed_slot_decision(
        self,
        *,
        run_attempt_id: str,
        slot_id: str,
        value_ref: str,
        original_user_text: str,
        binding_kind: str,
        provider_ref: str,
        model_ref: str,
        raw_provider_output: Mapping[str, Any],
        accepted_attempt_refs: Sequence[str],
    ) -> dict[str, Any]:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
            canonical_value,
        )

        if binding_kind not in {"fill_current_slot", "revise_current_slot"}:
            raise EvidenceIntegrityError("typed_slot_binding_kind_invalid")
        active = self.resolve_active_intent_revision(run_attempt_id)
        if active is None:
            raise EvidenceIntegrityError("decision_intent_not_active")
        option = None
        for candidate in self.load_decision_options(active.intent_revision_id):
            candidate_slot_id = str(candidate.get("slot_id") or "")
            try:
                _, candidate_value_ref = normalize_temporal_decision_value(
                    slot_id=candidate_slot_id,
                    value=candidate.get("typed_value"),
                    time_spec=active.time_spec,
                )
            except (TypeError, ValueError) as exc:
                raise EvidenceIntegrityError(
                    "decision_option_typed_value_invalid"
                ) from exc
            if candidate_slot_id == slot_id and candidate_value_ref == value_ref:
                option = candidate
                break
        if option is None:
            raise EvidenceIntegrityError("typed_slot_value_ref_unknown")
        slot = next(
            (item for item in active.ambiguity_slots if item.get("slot_id") == slot_id),
            None,
        )
        if slot is None:
            raise EvidenceIntegrityError("decision_option_slot_unknown")
        ledger = self.load_decision_ledger(active.intent_revision_id)
        prior = ledger.active_for_slot(slot_id)
        if binding_kind == "fill_current_slot" and prior is not None:
            raise EvidenceIntegrityError("typed_slot_already_resolved")
        if binding_kind == "revise_current_slot" and prior is None:
            raise EvidenceIntegrityError("typed_slot_revision_missing_prior")
        affected_by_kind = {
            "baseline": ("baseline_refs", "resolved_window_refs"),
            "comparison_window": ("baseline_refs", "resolved_window_refs"),
            "time": ("time_spec", "resolved_window_refs"),
            "scope": ("scope", "filters"),
            "metric": ("target_metric_refs", "analysis_axes"),
            "goal": ("goal_bindings", "desired_decisions"),
        }
        decision = DecisionRecord.create(
            intent_revision_id=active.intent_revision_id,
            slot_id=slot_id,
            value=option["typed_value"],
            source="user",
            status="user_confirmed",
            materiality=str(slot.get("materiality") or "material"),
            affected_plan_fields=affected_by_kind.get(
                str(slot.get("slot_kind") or ""),
                (f"ambiguity_slot:{slot_id}",),
            ),
            option_id=str(option["option_id"]),
            supersedes_decision_id=(
                prior.decision_id
                if prior is not None and prior.option_id != str(option["option_id"])
                else None
            ),
        )
        input_payload = {
            "intent_revision_id": active.intent_revision_id,
            "binding_kind": binding_kind,
            "slot_id": slot_id,
            "value_ref": value_ref,
            "original_user_text": original_user_text,
        }
        output_payload = {
            "decision": decision.to_dict(),
            "raw_provider_output": canonical_value(raw_provider_output),
        }
        existing = next(
            (
                item
                for item in ledger.records
                if item.decision_id == decision.decision_id
            ),
            None,
        )
        ledger_position = (
            self._decision_ledger_position(existing.decision_id)
            if existing is not None
            else ledger.position + 1
        )
        accepted = self.load_accepted_transition(
            run_attempt_id=run_attempt_id,
            node_name="bind_free_text_decision",
            input_digest=canonical_digest(input_payload),
        )
        if accepted is not None:
            self.attempt_journal.load_stage_attempt_refs(
                run_attempt_id=run_attempt_id,
                transition_attempt_id=accepted["transition"].attempt_id,
                stage_name="bind_free_text_submission",
            )
            return {
                "decision": decision.to_dict(),
                "decision_ledger_position": accepted[
                    "transition"
                ].decision_ledger_position,
                "durable_checkpoint": accepted["transition"].to_dict(),
                "replayed": True,
            }
        transition = DurableTransition.create(
            node_name="bind_free_text_decision",
            parent_transition_id=self.latest_accepted_transition_id(run_attempt_id),
            run_attempt_id=run_attempt_id,
            intent_revision_id=active.intent_revision_id,
            decision_ledger_position=ledger_position,
            input_digest=canonical_digest(input_payload),
            output_digest=canonical_digest(output_payload),
            execution_attempt=1,
            provider_ref=provider_ref,
            model_ref=model_ref,
            status="succeeded",
            acceptance_state="accepted",
            next_transition="compile_authoritative_plan",
        )
        stored, stored_position = self.append_decision_record_transition(
            decision=decision,
            transition=transition,
            input_payload=input_payload,
            output_payload=output_payload,
            provider_attempt_refs=accepted_attempt_refs,
        )
        lifecycle = self.latest_lifecycle_state(run_attempt_id)
        if lifecycle is not None and lifecycle.interaction_state == "waiting_for_user":
            self.append_lifecycle_state(
                lifecycle.transition(interaction_state="active")
            )
        return {
            "decision": stored.to_dict(),
            "decision_ledger_position": stored_position,
            "durable_checkpoint": transition.to_dict(),
            "replayed": False,
        }

    def save_interaction_directive_transition(
        self,
        *,
        directive: InteractionDirective,
        transition: DurableTransition,
        input_payload: Mapping[str, Any],
        output_payload: Mapping[str, Any],
        accepted_attempt_refs: Sequence[str],
        material_revision_continuation: MaterialRevisionContinuation | None = None,
    ) -> dict[str, Any]:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        if (
            transition.run_attempt_id != directive.run_attempt_id
            or transition.intent_revision_id != directive.intent_revision_id
            or transition.acceptance_state != "accepted"
        ):
            raise EvidenceIntegrityError("directive_transition_mismatch")
        if (
            isinstance(accepted_attempt_refs, (str, bytes))
            or len(tuple(accepted_attempt_refs)) != 1
        ):
            raise EvidenceIntegrityError(
                "directive_provider_attempt_cardinality_invalid"
            )
        normalized_attempt_refs = tuple(accepted_attempt_refs)
        if (
            directive.kind == "material_intent_change"
            and type(material_revision_continuation) is not MaterialRevisionContinuation
        ) or (
            directive.kind != "material_intent_change"
            and material_revision_continuation is not None
        ):
            raise EvidenceIntegrityError("material_revision_continuation_mismatch")
        source_dispatch = self._active_run_dispatches.get(directive.run_attempt_id)
        if source_dispatch is None:
            raise EvidenceIntegrityError(
                "interaction_directive_source_dispatch_missing"
            )
        material_result: dict[str, Any] | None = None
        directive_terminal_result: dict[str, Any] | None = None
        try:
            self._lock_active_run_dispatch(
                dispatch_id=source_dispatch[0],
                run_id=directive.run_attempt_id,
                dispatch_owner_id=source_dispatch[1],
                lease_epoch=source_dispatch[2],
                expected_producer_kind="clarification_resolution",
            )
            self._lock_single_authority_run(directive.run_attempt_id)
            active = self.resolve_active_intent_revision(directive.run_attempt_id)
            if (
                active is None
                or active.intent_revision_id != directive.intent_revision_id
            ):
                raise EvidenceIntegrityError("directive_intent_not_active")
            payload = directive.to_dict()
            inserted = self._execute(
                """
                INSERT INTO waje_runtime.interaction_directives(
                  directive_id, run_attempt_id, intent_revision_id, kind,
                  target_refs, original_user_text, source, content_digest,
                  payload
                ) VALUES (
                  %(directive_id)s, %(run_attempt_id)s,
                  %(intent_revision_id)s, %(kind)s, %(target_refs)s::jsonb,
                  %(original_user_text)s, %(source)s, %(content_digest)s,
                  %(payload)s::jsonb
                )
                ON CONFLICT DO NOTHING
                RETURNING directive_id
                """,
                {
                    **payload,
                    "target_refs": _json(payload["target_refs"]),
                    "payload": _json(payload),
                },
                commit=False,
            ).fetchone()
            stored = self._fetchone(
                """
                SELECT payload
                FROM waje_runtime.interaction_directives
                WHERE directive_id = %(directive_id)s
                """,
                {"directive_id": directive.directive_id},
            )
            if stored is None or canonical_value(
                _json_value(_field(stored, "payload", 0)) or {}
            ) != canonical_value(payload):
                raise EvidenceIntegrityError("interaction_directive_immutable_conflict")
            transition_status = self._save_transition_attempt_locked(
                transition=transition,
                input_payload=input_payload,
                output_payload=output_payload,
            )
            self.attempt_journal.bind_stage(
                run_attempt_id=directive.run_attempt_id,
                transition_attempt_id=transition.attempt_id,
                stage_name="bind_free_text_submission",
                attempt_refs=normalized_attempt_refs,
                commit=False,
            )
            if directive.kind in {"cancel", "challenge"}:
                directive_terminal_result = (
                    self._persist_control_directive_terminal_locked(
                        directive=directive,
                        source_dispatch=source_dispatch,
                    )
                )
            if material_revision_continuation is not None:
                material_result = self._persist_material_revision_continuation_locked(
                    directive=directive,
                    transition=transition,
                    continuation=material_revision_continuation,
                    source_dispatch=source_dispatch,
                )
            self.connection.commit()
            result = {
                "directive": payload,
                "durable_checkpoint": transition.to_dict(),
                "replayed": inserted is None and transition_status == "replayed",
            }
            if material_result is not None:
                result.update(material_result)
            if directive_terminal_result is not None:
                result.update(directive_terminal_result)
        except Exception:
            self.connection.rollback()
            raise
        if material_result is not None or directive_terminal_result is not None:
            self._stop_run_dispatch_heartbeat(source_dispatch[0])
            self._active_run_dispatches.pop(directive.run_attempt_id, None)
        return result

    def _persist_control_directive_terminal_locked(
        self,
        *,
        directive: InteractionDirective,
        source_dispatch: tuple[str, str, int],
    ) -> dict[str, Any]:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        if directive.kind not in {"cancel", "challenge"}:
            raise EvidenceIntegrityError("control_directive_kind_invalid")
        source = self._fetchone(
            """
            /* control_directive_source_lock */
            SELECT thread_id, turn_id, topic_id, status, request
            FROM waje_runtime.analysis_runs
            WHERE run_id = %(run_id)s
            FOR UPDATE
            """,
            {"run_id": directive.run_attempt_id},
        )
        source_thread_id = str(_field(source, "thread_id", 0) or "")
        source_turn_id = str(_field(source, "turn_id", 1) or "")
        source_topic_id = str(_field(source, "topic_id", 2) or "")
        waiting_request = _json_value(_field(source, "request", 4))
        runtime_descriptors = (
            waiting_request.get("runtime_descriptors")
            if isinstance(waiting_request, Mapping)
            else None
        )
        context_manifest = (
            runtime_descriptors.get("context_manifest")
            if isinstance(runtime_descriptors, Mapping)
            else None
        )
        pending = self._fetchone(
            """
            /* control_directive_pending_clarification_lock */
            SELECT pending_clarification_topic_id, pending_clarification_id
            FROM waje_runtime.investigation_threads
            WHERE thread_id = %(thread_id)s
            FOR UPDATE
            """,
            {"thread_id": source_thread_id},
        )
        current_lifecycle = self._latest_lifecycle_state_locked(
            directive.run_attempt_id
        )
        if (
            source is None
            or not source_thread_id
            or not source_turn_id
            or str(_field(source, "status", 3) or "") != "waiting_for_clarification"
            or pending is None
            or str(_field(pending, "pending_clarification_id", 1) or "")
            != directive.run_attempt_id
            or str(_field(pending, "pending_clarification_topic_id", 0) or "")
            != source_topic_id
            or current_lifecycle is None
            or current_lifecycle.execution_state != "waiting"
            or current_lifecycle.interaction_state != "waiting_for_user"
            or current_lifecycle.cancellation_state != "active"
            or current_lifecycle.supersession_state != "active"
            or not isinstance(context_manifest, Mapping)
            or not str(context_manifest.get("manifest_id") or "")
        ):
            raise EvidenceIntegrityError("control_directive_source_state_invalid")

        if directive.kind == "challenge":
            self._terminalize_active_run_dispatch(
                dispatch_id=source_dispatch[0],
                run_id=directive.run_attempt_id,
                dispatch_owner_id=source_dispatch[1],
                lease_epoch=source_dispatch[2],
                status="waiting_for_clarification",
            )
            self._audit(
                "single_authority_challenge_recorded",
                thread_id=source_thread_id,
                topic_id=source_topic_id or None,
                run_id=directive.run_attempt_id,
                ref=directive.directive_id,
                payload={
                    "directive_id": directive.directive_id,
                    "target_refs": list(directive.target_refs),
                },
                commit=False,
            )
            self._audit(
                "run_dispatch_completed",
                thread_id=source_thread_id,
                topic_id=source_topic_id or None,
                run_id=directive.run_attempt_id,
                ref=source_dispatch[0],
                payload={
                    "dispatch_id": source_dispatch[0],
                    "status": "waiting_for_clarification",
                    "lease_epoch": source_dispatch[2],
                },
                commit=False,
            )
            return {
                "source_waiting": {
                    "status": "waiting_for_clarification",
                    "run_id": directive.run_attempt_id,
                    "turn_id": source_turn_id,
                    "topic_id": source_topic_id or None,
                }
            }

        interaction_result = {
            "schema_version": "typed-interaction.v1",
            "intent": "analysis_cancellation",
            "response_text": "已取消当前分析。",
        }
        updated_source = self._execute(
            """
            /* cancellation_source_complete_cas */
            UPDATE waje_runtime.analysis_runs
            SET status = 'interaction_completed',
                request = %(request)s::jsonb, updated_at = now()
            WHERE run_id = %(run_id)s
              AND thread_id = %(thread_id)s
              AND status = 'waiting_for_clarification'
            RETURNING run_id
            """,
            {
                "run_id": directive.run_attempt_id,
                "thread_id": source_thread_id,
                "request": _json(
                    canonical_value(
                        {
                            **dict(waiting_request),
                            "interaction_result": interaction_result,
                        }
                    )
                ),
            },
            commit=False,
        ).fetchone()
        cleared = self._execute(
            """
            /* cancellation_pending_clarification_clear_cas */
            UPDATE waje_runtime.investigation_threads
            SET pending_clarification_topic_id = NULL,
                pending_clarification_id = '', updated_at = now()
            WHERE thread_id = %(thread_id)s
              AND pending_clarification_id = %(run_id)s
            RETURNING thread_id
            """,
            {
                "thread_id": source_thread_id,
                "run_id": directive.run_attempt_id,
            },
            commit=False,
        ).fetchone()
        if updated_source is None or cleared is None:
            raise EvidenceIntegrityError("control_directive_source_state_invalid")
        cancelled_lifecycle = current_lifecycle.transition(
            execution_state="cancelled",
            interaction_state="closed",
            cancellation_state="cancelled",
        )
        self._append_lifecycle_state_locked(cancelled_lifecycle)
        self._terminalize_active_run_dispatch(
            dispatch_id=source_dispatch[0],
            run_id=directive.run_attempt_id,
            dispatch_owner_id=source_dispatch[1],
            lease_epoch=source_dispatch[2],
            status="interaction_completed",
        )
        for event_type, ref, event_payload in (
            (
                "single_authority_run_cancelled",
                directive.directive_id,
                {
                    "directive_id": directive.directive_id,
                    "lifecycle_state_digest": cancelled_lifecycle.content_digest,
                },
            ),
            (
                "run_status_changed",
                directive.run_attempt_id,
                {"status": "interaction_completed"},
            ),
            (
                "clarification_cleared",
                directive.run_attempt_id,
                {"directive_id": directive.directive_id},
            ),
            (
                "run_dispatch_completed",
                source_dispatch[0],
                {
                    "dispatch_id": source_dispatch[0],
                    "status": "interaction_completed",
                    "lease_epoch": source_dispatch[2],
                },
            ),
        ):
            self._audit(
                event_type,
                thread_id=source_thread_id,
                topic_id=source_topic_id or None,
                run_id=directive.run_attempt_id,
                ref=ref,
                payload=event_payload,
                commit=False,
            )
        return {
            "source_terminal": canonical_value(
                {
                    "status": "interaction_completed",
                    "run_id": directive.run_attempt_id,
                    "turn_id": source_turn_id,
                    "topic_id": source_topic_id or None,
                    "intent": "analysis_cancellation",
                    "topic_relation": "analysis_cancellation",
                    "context_manifest": context_manifest,
                    "interaction_result": interaction_result,
                }
            )
        }

    def _persist_material_revision_continuation_locked(
        self,
        *,
        directive: InteractionDirective,
        transition: DurableTransition,
        continuation: MaterialRevisionContinuation,
        source_dispatch: tuple[str, str, int],
    ) -> dict[str, Any]:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        if (
            continuation.directive_id != directive.directive_id
            or continuation.source_run_id != directive.run_attempt_id
            or continuation.source_intent_revision_id != directive.intent_revision_id
            or continuation.parent_transition_id != transition.transition_id
        ):
            raise EvidenceIntegrityError("material_revision_continuation_mismatch")
        source = self._fetchone(
            """
            SELECT thread_id, turn_id, topic_id, status, request
            FROM waje_runtime.analysis_runs
            WHERE run_id = %(run_id)s
            FOR UPDATE
            """,
            {"run_id": directive.run_attempt_id},
        )
        waiting_request = _json_value(_field(source, "request", 4))
        runtime_descriptors = (
            waiting_request.get("runtime_descriptors")
            if isinstance(waiting_request, Mapping)
            else None
        )
        context_manifest = (
            runtime_descriptors.get("context_manifest")
            if isinstance(runtime_descriptors, Mapping)
            else None
        )
        source_thread_id = str(_field(source, "thread_id", 0) or "")
        source_turn_id = str(_field(source, "turn_id", 1) or "")
        source_topic_id = str(_field(source, "topic_id", 2) or "")
        if (
            source is None
            or source_thread_id != continuation.thread_id
            or str(_field(source, "status", 3) or "") != "waiting_for_clarification"
            or not source_turn_id
            or not isinstance(context_manifest, Mapping)
            or not str(context_manifest.get("manifest_id") or "")
        ):
            raise EvidenceIntegrityError("material_revision_source_state_invalid")

        interaction_result = {
            "schema_version": "typed-interaction.v1",
            "intent": "material_revision",
            "response_text": "已接受业务问题修订，后续分析已创建并继续执行。",
        }
        continuation_payload = continuation.to_dict()
        source_request = canonical_value(
            {
                "interaction_result": interaction_result,
                "material_revision_continuation": continuation_payload,
            }
        )
        successor_owner_id = f"material-revision-dispatch-{uuid4()}"
        successor_run = self._execute(
            """
            /* material_revision_successor_run_insert */
            INSERT INTO waje_runtime.analysis_runs(
              run_id, run_attempt_id, thread_id, status, request
            ) VALUES (
              %(run_id)s, %(run_id)s, %(thread_id)s, 'queued', '{}'::jsonb
            )
            ON CONFLICT DO NOTHING
            RETURNING run_id
            """,
            {
                "run_id": continuation.successor_run_id,
                "thread_id": continuation.thread_id,
            },
            commit=False,
        ).fetchone()
        successor_message = self._execute(
            """
            /* material_revision_successor_message_insert */
            INSERT INTO waje_runtime.conversation_messages(
              message_id, thread_id, role, text
            ) VALUES (
              %(message_id)s, %(thread_id)s, 'user', %(text)s
            )
            ON CONFLICT DO NOTHING
            RETURNING message_id
            """,
            {
                "message_id": continuation.successor_message_id,
                "thread_id": continuation.thread_id,
                "text": continuation.successor_user_text,
            },
            commit=False,
        ).fetchone()
        successor_dispatch = self._execute(
            """
            /* material_revision_successor_dispatch_insert */
            INSERT INTO waje_runtime.run_dispatches(
              dispatch_id, producer_kind, scope_ref, request_identity,
              request_digest, request_payload, thread_id, run_id,
              message_id, dispatch_state, owner_id, lease_epoch,
              lease_expires_at, heartbeat_at
            ) VALUES (
              %(dispatch_id)s, %(producer_kind)s, %(scope_ref)s,
              %(request_identity)s, %(request_digest)s,
              %(request_payload)s::jsonb, %(thread_id)s, %(run_id)s,
              %(message_id)s, 'leased', %(owner_id)s, 1,
              now() + (%(lease_ms)s * interval '1 millisecond'), now()
            )
            ON CONFLICT DO NOTHING
            RETURNING dispatch_id, lease_epoch
            """,
            {
                "dispatch_id": continuation.successor_dispatch_id,
                "producer_kind": continuation.producer_kind,
                "scope_ref": continuation.scope_ref,
                "request_identity": continuation.request_identity,
                "request_digest": continuation.request_digest,
                "request_payload": _json(continuation.request_payload),
                "thread_id": continuation.thread_id,
                "run_id": continuation.successor_run_id,
                "message_id": continuation.successor_message_id,
                "owner_id": successor_owner_id,
                "lease_ms": _run_dispatch_lease_ms(),
            },
            commit=False,
        ).fetchone()
        if (
            successor_run is None
            or successor_message is None
            or successor_dispatch is None
            or str(_field(successor_dispatch, "dispatch_id", 0) or "")
            != continuation.successor_dispatch_id
            or int(_field(successor_dispatch, "lease_epoch", 1) or 0) != 1
        ):
            raise EvidenceIntegrityError("material_revision_successor_conflict")

        updated_source = self._execute(
            """
            /* material_revision_source_complete_cas */
            UPDATE waje_runtime.analysis_runs
            SET status = 'interaction_completed',
                request = %(request)s::jsonb, updated_at = now()
            WHERE run_id = %(run_id)s
              AND thread_id = %(thread_id)s
              AND status = 'waiting_for_clarification'
            RETURNING run_id
            """,
            {
                "run_id": directive.run_attempt_id,
                "thread_id": continuation.thread_id,
                "request": _json(source_request),
            },
            commit=False,
        ).fetchone()
        cleared = self._execute(
            """
            /* material_revision_pending_clarification_clear_cas */
            UPDATE waje_runtime.investigation_threads
            SET pending_clarification_topic_id = NULL,
                pending_clarification_id = '', updated_at = now()
            WHERE thread_id = %(thread_id)s
              AND pending_clarification_id = %(source_run_id)s
            RETURNING thread_id
            """,
            {
                "thread_id": continuation.thread_id,
                "source_run_id": directive.run_attempt_id,
            },
            commit=False,
        ).fetchone()
        current_lifecycle = self._latest_lifecycle_state_locked(
            directive.run_attempt_id
        )
        if (
            updated_source is None
            or cleared is None
            or current_lifecycle is None
            or current_lifecycle.execution_state != "waiting"
            or current_lifecycle.interaction_state != "waiting_for_user"
        ):
            raise EvidenceIntegrityError("material_revision_source_state_invalid")
        self._append_lifecycle_state_locked(
            current_lifecycle.transition(
                execution_state="superseded",
                interaction_state="superseded",
                supersession_state="superseded",
            )
        )
        self._terminalize_active_run_dispatch(
            dispatch_id=source_dispatch[0],
            run_id=directive.run_attempt_id,
            dispatch_owner_id=source_dispatch[1],
            lease_epoch=source_dispatch[2],
            status="interaction_completed",
        )
        source_link = continuation.source_link_payload
        for event_type, run_id, ref, event_payload in (
            (
                "material_revision_continuation_created",
                directive.run_attempt_id,
                continuation.continuation_ref,
                source_link,
            ),
            (
                "run_status_changed",
                directive.run_attempt_id,
                directive.run_attempt_id,
                {
                    "status": "interaction_completed",
                    "successor_run_id": continuation.successor_run_id,
                },
            ),
            (
                "clarification_cleared",
                directive.run_attempt_id,
                directive.run_attempt_id,
                source_link,
            ),
            (
                "run_dispatch_completed",
                directive.run_attempt_id,
                source_dispatch[0],
                {
                    "dispatch_id": source_dispatch[0],
                    "status": "interaction_completed",
                    "lease_epoch": source_dispatch[2],
                },
            ),
            (
                "message_recorded",
                continuation.successor_run_id,
                continuation.successor_message_id,
                source_link,
            ),
            (
                "run_queued",
                continuation.successor_run_id,
                continuation.successor_run_id,
                source_link,
            ),
            (
                "run_dispatch_leased",
                continuation.successor_run_id,
                continuation.successor_dispatch_id,
                {
                    **source_link,
                    "dispatch_owner_id": successor_owner_id,
                    "lease_epoch": 1,
                },
            ),
        ):
            self._audit(
                event_type,
                thread_id=continuation.thread_id,
                topic_id=source_topic_id or None,
                run_id=run_id,
                ref=ref,
                payload=event_payload,
                commit=False,
            )
        return {
            "material_revision_continuation": continuation_payload,
            "successor_run_dispatch": {
                "dispatch_id": continuation.successor_dispatch_id,
                "dispatch_owner_id": successor_owner_id,
                "lease_epoch": 1,
            },
            "source_terminal": {
                "status": "interaction_completed",
                "run_id": directive.run_attempt_id,
                "turn_id": source_turn_id,
                "topic_id": source_topic_id or None,
                "intent": "material_revision",
                "topic_relation": "material_revision",
                "context_manifest": canonical_value(context_manifest),
                "interaction_result": interaction_result,
            },
        }

    def latest_accepted_transition_id(self, run_attempt_id: str) -> str | None:
        row = self._fetchone(
            """
            SELECT transition_id
            FROM waje_runtime.workflow_transition_attempts
            WHERE run_attempt_id = %(run_attempt_id)s
              AND acceptance_state = 'accepted'
            ORDER BY created_at DESC, attempt_id DESC
            LIMIT 1
            """,
            {"run_attempt_id": run_attempt_id},
        )
        return str(_field(row, "transition_id", 0) or "") or None

    def _decision_ledger_position(self, decision_id: str) -> int:
        row = self._fetchone(
            """
            SELECT ledger_position
            FROM waje_runtime.decision_records
            WHERE decision_id = %(decision_id)s
            """,
            {"decision_id": decision_id},
        )
        if row is None:
            from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

            raise EvidenceIntegrityError("decision_ledger_position_missing")
        return int(_field(row, "ledger_position", 0) or 0)

    def load_accepted_transition(
        self,
        *,
        run_attempt_id: str,
        node_name: str,
        input_digest: str,
    ) -> dict[str, Any] | None:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
            canonical_value,
        )

        rows = self._fetchall(
            """
            SELECT transition_id, attempt_id, node_name, parent_transition_id,
                   run_attempt_id, intent_revision_id,
                   decision_ledger_position, input_digest, output_digest,
                   execution_attempt, provider_ref, model_ref, status,
                   acceptance_state, next_transition, started_at, finished_at,
                   input_payload, output_payload
            FROM waje_runtime.workflow_transition_attempts
            WHERE run_attempt_id = %(run_attempt_id)s
              AND node_name = %(node_name)s
              AND input_digest = %(input_digest)s
              AND acceptance_state = 'accepted'
            ORDER BY execution_attempt DESC
            LIMIT 2
            """,
            {
                "run_attempt_id": run_attempt_id,
                "node_name": node_name,
                "input_digest": input_digest,
            },
        )
        if len(rows) > 1:
            raise EvidenceIntegrityError("accepted_transition_ambiguous")
        if not rows:
            return None
        row = rows[0]

        def timestamp(value: Any) -> str:
            return value.isoformat() if hasattr(value, "isoformat") else str(value)

        transition = DurableTransition.from_dict(
            {
                "transition_id": str(_field(row, "transition_id", 0) or ""),
                "attempt_id": str(_field(row, "attempt_id", 1) or ""),
                "node_name": str(_field(row, "node_name", 2) or ""),
                "parent_transition_id": _field(row, "parent_transition_id", 3),
                "run_attempt_id": str(_field(row, "run_attempt_id", 4) or ""),
                "intent_revision_id": str(_field(row, "intent_revision_id", 5) or ""),
                "decision_ledger_position": int(
                    _field(row, "decision_ledger_position", 6) or 0
                ),
                "input_digest": str(_field(row, "input_digest", 7) or ""),
                "output_digest": str(_field(row, "output_digest", 8) or ""),
                "execution_attempt": int(_field(row, "execution_attempt", 9) or 0),
                "provider_ref": str(_field(row, "provider_ref", 10) or ""),
                "model_ref": str(_field(row, "model_ref", 11) or ""),
                "status": str(_field(row, "status", 12) or ""),
                "acceptance_state": str(_field(row, "acceptance_state", 13) or ""),
                "next_transition": str(_field(row, "next_transition", 14) or ""),
                "started_at": timestamp(_field(row, "started_at", 15)),
                "finished_at": timestamp(_field(row, "finished_at", 16)),
            }
        )
        input_payload = _json_value(_field(row, "input_payload", 17))
        output_payload = _json_value(_field(row, "output_payload", 18))
        if (
            not isinstance(input_payload, Mapping)
            or not isinstance(output_payload, Mapping)
            or canonical_digest(input_payload) != transition.input_digest
            or canonical_digest(output_payload) != transition.output_digest
        ):
            raise EvidenceIntegrityError("accepted_transition_payload_invalid")
        return {
            "transition": transition,
            "input_payload": canonical_value(input_payload),
            "output_payload": canonical_value(output_payload),
        }

    def load_accepted_free_text_submission(
        self,
        *,
        run_attempt_id: str,
        original_user_text: str,
    ) -> dict[str, Any] | None:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        rows = self._fetchall(
            """
            SELECT node_name, input_payload, output_payload, input_digest
            FROM waje_runtime.workflow_transition_attempts
            WHERE run_attempt_id = %(run_attempt_id)s
              AND node_name IN (
                'bind_free_text_decision', 'bind_free_text_directive'
              )
              AND acceptance_state = 'accepted'
              AND input_payload ->> 'original_user_text'
                  = %(original_user_text)s
            ORDER BY created_at DESC, attempt_id DESC
            LIMIT 2
            """,
            {
                "run_attempt_id": run_attempt_id,
                "original_user_text": original_user_text,
            },
        )
        if len(rows) > 1:
            raise EvidenceIntegrityError(
                "free_text_submission_accepted_transition_ambiguous"
            )
        if not rows:
            return None
        node_name = str(_field(rows[0], "node_name", 0) or "")
        input_digest = str(_field(rows[0], "input_digest", 3) or "")
        accepted = self.load_accepted_transition(
            run_attempt_id=run_attempt_id,
            node_name=node_name,
            input_digest=input_digest,
        )
        if accepted is None:
            raise EvidenceIntegrityError("free_text_submission_transition_missing")
        return accepted

    def save_failure_record(
        self, *, run_attempt_id: str, failure: FailureRecord
    ) -> str:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        try:
            self._lock_single_authority_run(run_attempt_id)
            payload = failure.to_dict()
            inserted = self._execute(
                """
                INSERT INTO waje_runtime.failure_records(
                  failure_id, run_attempt_id, layer, kind, scope,
                  affected_refs, integrity_level, retryability,
                  user_actionable, business_boundary, technical_detail_ref,
                  content_digest, payload
                ) VALUES (
                  %(failure_id)s, %(run_attempt_id)s, %(layer)s, %(kind)s,
                  %(scope)s, %(affected_refs)s::jsonb, %(integrity_level)s,
                  %(retryability)s, %(user_actionable)s, %(business_boundary)s,
                  %(technical_detail_ref)s, %(content_digest)s,
                  %(payload)s::jsonb
                )
                ON CONFLICT DO NOTHING
                RETURNING failure_id
                """,
                {
                    **payload,
                    "run_attempt_id": run_attempt_id,
                    "affected_refs": _json(payload["affected_refs"]),
                    "payload": _json(payload),
                },
                commit=False,
            ).fetchone()
            stored = self._fetchone(
                """
                SELECT payload
                FROM waje_runtime.failure_records
                WHERE run_attempt_id = %(run_attempt_id)s
                  AND failure_id = %(failure_id)s
                """,
                {"run_attempt_id": run_attempt_id, "failure_id": failure.failure_id},
            )
            if stored is None or canonical_value(
                _json_value(_field(stored, "payload", 0)) or {}
            ) != canonical_value(payload):
                raise EvidenceIntegrityError("failure_record_immutable_conflict")
            self.connection.commit()
            return "inserted" if inserted is not None else "replayed"
        except Exception:
            self.connection.rollback()
            raise

    def append_lifecycle_state(self, state: LifecycleState) -> str:
        try:
            self._lock_single_authority_run(state.run_attempt_id)
            result = self._append_lifecycle_state_locked(state)
            self.connection.commit()
            return result
        except Exception:
            self.connection.rollback()
            raise

    def cancel_run_attempt(
        self,
        *,
        run_attempt_id: str,
        reason_ref: str,
    ) -> dict[str, Any]:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        if not isinstance(reason_ref, str) or not reason_ref.strip():
            raise EvidenceIntegrityError("cancellation_reason_ref_invalid")
        try:
            self._lock_single_authority_run(run_attempt_id)
            current = self._latest_lifecycle_state_locked(run_attempt_id)
            if current is None:
                raise EvidenceIntegrityError("cancellation_lifecycle_missing")
            if current.cancellation_state == "cancelled":
                self.connection.commit()
                return {
                    "lifecycle": current.to_dict(),
                    "reason_ref": reason_ref,
                    "replayed": True,
                }
            cancelled = current.transition(
                execution_state="cancelled",
                interaction_state="closed",
                cancellation_state="cancelled",
            )
            self._append_lifecycle_state_locked(cancelled)
            self._audit(
                "single_authority_run_cancelled",
                run_id=run_attempt_id,
                ref=reason_ref,
                payload={
                    "reason_ref": reason_ref,
                    "lifecycle_state_digest": cancelled.content_digest,
                },
                commit=False,
            )
            self.connection.commit()
            return {
                "lifecycle": cancelled.to_dict(),
                "reason_ref": reason_ref,
                "replayed": False,
            }
        except Exception:
            self.connection.rollback()
            raise

    def latest_lifecycle_state(self, run_attempt_id: str) -> LifecycleState | None:
        return self._latest_lifecycle_state_locked(run_attempt_id)

    def load_post_seal_failure_terminal(
        self,
        *,
        authority_bundle: Any,
        authority_transition: Any,
    ) -> Any:
        from bi_agent.runtime.post_seal_failure_persistence import (
            load_post_seal_failure_terminal,
        )

        return load_post_seal_failure_terminal(
            self.connection,
            authority_bundle=authority_bundle,
            authority_transition=authority_transition,
        )

    def record_post_seal_failure(
        self,
        *,
        owner_ref: str,
        thread_ref: str,
        authority_bundle: Any,
        authority_transition: Any,
        status: str,
        failure_record: FailureRecord,
        supersedes_terminal_ref: str | None,
    ) -> Any:
        from bi_agent.runtime.post_seal_failure_persistence import (
            record_post_seal_failure,
        )

        return record_post_seal_failure(
            self.connection,
            owner_ref=owner_ref,
            thread_ref=thread_ref,
            authority_bundle=authority_bundle,
            authority_transition=authority_transition,
            status=status,
            failure_record=failure_record,
            supersedes_terminal_ref=supersedes_terminal_ref,
        )

    def record_orphaned_result(
        self,
        *,
        run_attempt_id: str,
        result_intent_revision_id: str,
        active_intent_revision_id: str | None,
        source_transition_id: str,
        result_ref: str,
        reason: str,
        payload: Mapping[str, Any],
    ) -> str:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
            canonical_value,
        )

        body = canonical_value(
            {
                "run_attempt_id": run_attempt_id,
                "result_intent_revision_id": result_intent_revision_id,
                "active_intent_revision_id": active_intent_revision_id,
                "source_transition_id": source_transition_id,
                "result_ref": result_ref,
                "reason": reason,
                "payload": payload,
            }
        )
        content_digest = canonical_digest(body)
        orphaned_result_id = (
            "orphaned-result-"
            + canonical_digest(
                {
                    "run_attempt_id": run_attempt_id,
                    "result_ref": result_ref,
                    "content_digest": content_digest,
                }
            )[:24]
        )
        try:
            self._lock_single_authority_run(run_attempt_id)
            inserted = self._execute(
                """
                INSERT INTO waje_runtime.orphaned_results(
                  orphaned_result_id, run_attempt_id,
                  result_intent_revision_id, active_intent_revision_id,
                  source_transition_id, result_ref, reason, content_digest,
                  payload
                ) VALUES (
                  %(orphaned_result_id)s, %(run_attempt_id)s,
                  %(result_intent_revision_id)s, %(active_intent_revision_id)s,
                  %(source_transition_id)s, %(result_ref)s, %(reason)s,
                  %(content_digest)s, %(payload)s::jsonb
                )
                ON CONFLICT DO NOTHING
                RETURNING orphaned_result_id
                """,
                {
                    **body,
                    "orphaned_result_id": orphaned_result_id,
                    "content_digest": content_digest,
                    "payload": _json(body["payload"]),
                },
                commit=False,
            ).fetchone()
            stored = self._fetchone(
                """
                SELECT content_digest, payload
                FROM waje_runtime.orphaned_results
                WHERE orphaned_result_id = %(orphaned_result_id)s
                """,
                {"orphaned_result_id": orphaned_result_id},
            )
            if (
                stored is None
                or str(_field(stored, "content_digest", 0) or "") != content_digest
                or canonical_value(_json_value(_field(stored, "payload", 1)) or {})
                != canonical_value(body["payload"])
            ):
                raise EvidenceIntegrityError("orphaned_result_immutable_conflict")
            self.connection.commit()
            return "inserted" if inserted is not None else "replayed"
        except Exception:
            self.connection.rollback()
            raise

    def assert_revision_can_publish(
        self, *, run_attempt_id: str, intent_revision_id: str
    ) -> dict[str, Any]:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError
        from bi_agent.runtime.single_authority import result_acceptance_state

        active = self.resolve_active_intent_revision(run_attempt_id)
        lifecycle = self.latest_lifecycle_state(run_attempt_id)
        if active is None or lifecycle is None:
            raise EvidenceIntegrityError("publication_authority_missing")
        acceptance = result_acceptance_state(
            lifecycle=lifecycle,
            result_intent_revision_id=intent_revision_id,
            active_intent_revision_id=active.intent_revision_id,
        )
        if acceptance != "accepted":
            raise EvidenceIntegrityError("publication_revision_not_active")
        return {
            "run_attempt_id": run_attempt_id,
            "intent_revision_id": active.intent_revision_id,
            "lifecycle_state_digest": lifecycle.content_digest,
            "acceptance_state": acceptance,
        }

    def _lock_single_authority_run(self, run_attempt_id: str) -> None:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        self._execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
            {"lock_key": f"single_authority:{run_attempt_id}"},
            commit=False,
        )
        row = self._fetchone(
            """
            SELECT run_id, run_attempt_id
            FROM waje_runtime.analysis_runs
            WHERE run_id = %(run_attempt_id)s
            FOR UPDATE
            """,
            {"run_attempt_id": run_attempt_id},
        )
        if (
            row is None
            or str(_field(row, "run_id", 0) or "") != run_attempt_id
            or str(_field(row, "run_attempt_id", 1) or "") != run_attempt_id
        ):
            raise EvidenceIntegrityError("single_authority_run_missing")

    def _build_authoritative_execution_result(
        self,
        *,
        plan_revision: PlanRevision,
        snapshot: Any,
        stop_record: Any,
        transition: DurableTransition,
    ):
        from bi_agent.runtime.authoritative_execution_result import (
            AuthoritativeExecutionResult,
        )

        bundles = tuple(
            bundle
            for task in plan_revision.capability_tasks
            if (
                bundle := self.load_capability_outcome(
                    plan_revision.plan_revision_id,
                    task.task_id,
                )
            )
            is not None
        )
        return AuthoritativeExecutionResult.from_records(
            plan_revision=plan_revision,
            execution_snapshot=snapshot,
            exploration_stop_record=stop_record,
            capability_outcome_bundles=bundles,
            durable_transition=transition,
        )

    def _persist_plan_result_refs_locked(
        self,
        *,
        run_attempt_id: str,
        plan_result_refs: Mapping[str, Any],
        transition_replayed: bool,
        previous_plan_result_refs: Mapping[str, Any] | None,
        plan_patch_ref: str | None,
    ) -> None:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        request = self._single_authority_request_locked(run_attempt_id)
        has_plan_refs = "plan_result_refs" in request
        existing_plan_refs = request.get("plan_result_refs")
        expected = canonical_value(plan_result_refs)
        if transition_replayed:
            if not has_plan_refs or canonical_value(existing_plan_refs) != expected:
                raise EvidenceIntegrityError("plan_result_refs_replay_conflict")
            return
        if previous_plan_result_refs is None:
            if (
                has_plan_refs
                or "execution_result_refs" in request
                or "claim_coverage_refs" in request
                or plan_patch_ref is not None
            ):
                raise EvidenceIntegrityError("plan_result_refs_conflict")
        elif not has_plan_refs or canonical_value(
            existing_plan_refs
        ) != canonical_value(previous_plan_result_refs):
            raise EvidenceIntegrityError("plan_result_refs_conflict")
        if previous_plan_result_refs is not None:
            execution_refs = request.get("execution_result_refs")
            coverage_refs = request.get("claim_coverage_refs")
            if (
                not isinstance(execution_refs, Mapping)
                or execution_refs.get("plan_revision_id")
                != previous_plan_result_refs.get("plan_revision_id")
                or not isinstance(coverage_refs, Mapping)
                or coverage_refs.get("source_plan_revision_id")
                != previous_plan_result_refs.get("plan_revision_id")
                or coverage_refs.get("plan_patch_ref") != plan_patch_ref
                or coverage_refs.get("decision") != "patch"
            ):
                raise EvidenceIntegrityError("plan_result_refs_conflict")
        next_request = dict(request)
        next_request.pop("execution_result_refs", None)
        next_request.pop("claim_coverage_refs", None)
        next_request["plan_result_refs"] = expected
        self._replace_single_authority_request_locked(
            run_attempt_id,
            current_request=request,
            next_request=next_request,
            conflict_code="plan_result_refs_conflict",
        )

    def _plan_result_refs_from_transition_locked(
        self,
        *,
        plan_revision: PlanRevision,
        accepted_transition_id: str,
    ) -> dict[str, Any]:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        row = self._fetchone(
            """
            SELECT node_name, input_payload, output_payload
            FROM waje_runtime.workflow_transition_attempts
            WHERE transition_id = %(transition_id)s
              AND run_attempt_id = %(run_attempt_id)s
              AND acceptance_state = 'accepted'
            """,
            {
                "transition_id": accepted_transition_id,
                "run_attempt_id": plan_revision.run_attempt_id,
            },
        )
        if row is None:
            raise EvidenceIntegrityError("plan_transition_refs_missing")
        node_name = str(_field(row, "node_name", 0) or "")
        transition_input = _json_value(_field(row, "input_payload", 1)) or {}
        transition_output = _json_value(_field(row, "output_payload", 2)) or {}
        expected_node = (
            "compile_plan_patch"
            if plan_revision.supersedes_plan_revision_id is not None
            else "compile_authoritative_plan"
        )
        plan_patch_ref = (
            transition_input.get("plan_patch_ref")
            if isinstance(transition_input, Mapping)
            else None
        )
        if (
            node_name != expected_node
            or not isinstance(transition_output, Mapping)
            or (transition_output.get("plan_revision") or {}).get("plan_revision_id")
            != plan_revision.plan_revision_id
            or (
                plan_revision.supersedes_plan_revision_id is None
                and plan_patch_ref is not None
            )
            or (
                plan_revision.supersedes_plan_revision_id is not None
                and (not isinstance(plan_patch_ref, str) or not plan_patch_ref)
            )
        ):
            raise EvidenceIntegrityError("plan_transition_refs_invalid")
        return _plan_result_refs_from_revision(
            plan_revision=plan_revision,
            accepted_transition_id=accepted_transition_id,
            plan_patch_ref=plan_patch_ref,
        )

    def _persist_execution_result_refs_locked(
        self,
        *,
        run_attempt_id: str,
        plan_result_refs: Mapping[str, Any],
        execution_result_refs: Mapping[str, Any],
        transition_replayed: bool,
    ) -> None:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        request = self._single_authority_request_locked(run_attempt_id)
        if canonical_value(request.get("plan_result_refs")) != canonical_value(
            plan_result_refs
        ):
            raise EvidenceIntegrityError("execution_result_plan_refs_conflict")
        has_execution_refs = "execution_result_refs" in request
        existing_execution_refs = request.get("execution_result_refs")
        expected = canonical_value(execution_result_refs)
        if transition_replayed:
            if (
                not has_execution_refs
                or canonical_value(existing_execution_refs) != expected
            ):
                raise EvidenceIntegrityError("execution_result_refs_replay_conflict")
            return
        if has_execution_refs:
            raise EvidenceIntegrityError("execution_result_refs_conflict")
        self._replace_single_authority_request_locked(
            run_attempt_id,
            current_request=request,
            next_request={**request, "execution_result_refs": expected},
            conflict_code="execution_result_refs_conflict",
        )

    def _single_authority_request_locked(self, run_attempt_id: str) -> dict[str, Any]:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        row = self._fetchone(
            """
            SELECT request
            FROM waje_runtime.analysis_runs
            WHERE run_id = %(run_attempt_id)s
              AND run_attempt_id = %(run_attempt_id)s
            """,
            {"run_attempt_id": run_attempt_id},
        )
        request = _json_value(_field(row, "request", 0)) if row else None
        if not isinstance(request, Mapping):
            raise EvidenceIntegrityError("single_authority_run_request_invalid")
        return dict(request)

    def _replace_single_authority_request_locked(
        self,
        run_attempt_id: str,
        *,
        current_request: Mapping[str, Any],
        next_request: Mapping[str, Any],
        conflict_code: str,
    ) -> None:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        updated = self._execute(
            """
            UPDATE waje_runtime.analysis_runs
            SET request = %(next_request)s::jsonb, updated_at = now()
            WHERE run_id = %(run_attempt_id)s
              AND run_attempt_id = %(run_attempt_id)s
              AND request = %(current_request)s::jsonb
            RETURNING run_id
            """,
            {
                "run_attempt_id": run_attempt_id,
                "current_request": _json(current_request),
                "next_request": _json(next_request),
            },
            commit=False,
        ).fetchone()
        if updated is None:
            raise EvidenceIntegrityError(conflict_code)

    def _save_transition_attempt_locked(
        self,
        *,
        transition: DurableTransition,
        input_payload: Mapping[str, Any],
        output_payload: Mapping[str, Any],
    ) -> str:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
            canonical_value,
        )

        if canonical_digest(input_payload) != transition.input_digest:
            raise EvidenceIntegrityError("transition_input_digest_mismatch")
        if canonical_digest(output_payload) != transition.output_digest:
            raise EvidenceIntegrityError("transition_output_digest_mismatch")
        params = {
            **transition.to_dict(),
            "intent_revision_id": transition.intent_revision_id or None,
            "input_payload": _json(canonical_value(input_payload)),
            "output_payload": _json(canonical_value(output_payload)),
        }
        inserted = self._execute(
            """
            INSERT INTO waje_runtime.workflow_transition_attempts(
              attempt_id, transition_id, node_name, parent_transition_id,
              run_attempt_id, intent_revision_id, decision_ledger_position,
              input_digest, output_digest, execution_attempt, provider_ref,
              model_ref, status, acceptance_state, next_transition,
              input_payload, output_payload, started_at, finished_at
            ) VALUES (
              %(attempt_id)s, %(transition_id)s, %(node_name)s,
              %(parent_transition_id)s, %(run_attempt_id)s,
              %(intent_revision_id)s, %(decision_ledger_position)s,
              %(input_digest)s, %(output_digest)s, %(execution_attempt)s,
              %(provider_ref)s, %(model_ref)s, %(status)s,
              %(acceptance_state)s, %(next_transition)s,
              %(input_payload)s::jsonb, %(output_payload)s::jsonb,
              %(started_at)s, %(finished_at)s
            )
            ON CONFLICT DO NOTHING
            RETURNING attempt_id
            """,
            params,
            commit=False,
        ).fetchone()
        if inserted is not None:
            return "inserted"
        stored = self._fetchone(
            """
            SELECT transition_id, node_name, parent_transition_id,
                   run_attempt_id, intent_revision_id,
                   decision_ledger_position, input_digest, output_digest,
                   execution_attempt, provider_ref, model_ref, status,
                   acceptance_state, next_transition, input_payload,
                   output_payload
            FROM waje_runtime.workflow_transition_attempts
            WHERE attempt_id = %(attempt_id)s
            """,
            {"attempt_id": transition.attempt_id},
        )
        if stored is None:
            accepted = self._fetchone(
                """
                SELECT attempt_id
                FROM waje_runtime.workflow_transition_attempts
                WHERE transition_id = %(transition_id)s
                  AND acceptance_state = 'accepted'
                """,
                {"transition_id": transition.transition_id},
            )
            if accepted is not None:
                raise EvidenceIntegrityError("transition_already_accepted")
            raise EvidenceIntegrityError("transition_attempt_immutable_conflict")
        actual = {
            "transition_id": str(_field(stored, "transition_id", 0) or ""),
            "node_name": str(_field(stored, "node_name", 1) or ""),
            "parent_transition_id": _field(stored, "parent_transition_id", 2),
            "run_attempt_id": str(_field(stored, "run_attempt_id", 3) or ""),
            "intent_revision_id": str(_field(stored, "intent_revision_id", 4) or ""),
            "decision_ledger_position": int(
                _field(stored, "decision_ledger_position", 5) or 0
            ),
            "input_digest": str(_field(stored, "input_digest", 6) or ""),
            "output_digest": str(_field(stored, "output_digest", 7) or ""),
            "execution_attempt": int(_field(stored, "execution_attempt", 8) or 0),
            "provider_ref": str(_field(stored, "provider_ref", 9) or ""),
            "model_ref": str(_field(stored, "model_ref", 10) or ""),
            "status": str(_field(stored, "status", 11) or ""),
            "acceptance_state": str(_field(stored, "acceptance_state", 12) or ""),
            "next_transition": str(_field(stored, "next_transition", 13) or ""),
            "input_payload": canonical_value(
                _json_value(_field(stored, "input_payload", 14)) or {}
            ),
            "output_payload": canonical_value(
                _json_value(_field(stored, "output_payload", 15)) or {}
            ),
        }
        expected = {
            key: value
            for key, value in transition.to_dict().items()
            if key
            in {
                "transition_id",
                "node_name",
                "parent_transition_id",
                "run_attempt_id",
                "intent_revision_id",
                "decision_ledger_position",
                "input_digest",
                "output_digest",
                "execution_attempt",
                "provider_ref",
                "model_ref",
                "status",
                "acceptance_state",
                "next_transition",
            }
        }
        expected["input_payload"] = canonical_value(input_payload)
        expected["output_payload"] = canonical_value(output_payload)
        if actual != expected:
            raise EvidenceIntegrityError("transition_attempt_immutable_conflict")
        return "replayed"

    def _latest_lifecycle_state_locked(
        self, run_attempt_id: str
    ) -> LifecycleState | None:
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        row = self._fetchone(
            """
            SELECT payload
            FROM waje_runtime.run_lifecycle_state_revisions
            WHERE run_attempt_id = %(run_attempt_id)s
            ORDER BY state_revision DESC
            LIMIT 1
            """,
            {"run_attempt_id": run_attempt_id},
        )
        if row is None:
            return None
        try:
            return LifecycleState.from_dict(
                _json_value(_field(row, "payload", 0)) or {}
            )
        except (TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("lifecycle_state_invalid") from exc

    def _append_lifecycle_state_locked(self, state: LifecycleState) -> str:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        current = self._latest_lifecycle_state_locked(state.run_attempt_id)
        if current is not None and current.content_digest == state.content_digest:
            return "replayed"
        if current is None:
            if state.state_revision != 1 or state.prior_state_digest is not None:
                raise EvidenceIntegrityError("lifecycle_initial_state_invalid")
        elif (
            state.state_revision != current.state_revision + 1
            or state.prior_state_digest != current.content_digest
        ):
            raise EvidenceIntegrityError("lifecycle_revision_conflict")
        payload = state.to_dict()
        inserted = self._execute(
            """
            INSERT INTO waje_runtime.run_lifecycle_state_revisions(
              run_attempt_id, state_revision, execution_state,
              interaction_state, evidence_state, publication_state,
              delivery_state, retry_state, cancellation_state,
              supersession_state, prior_state_digest, content_digest, payload
            ) VALUES (
              %(run_attempt_id)s, %(state_revision)s, %(execution_state)s,
              %(interaction_state)s, %(evidence_state)s,
              %(publication_state)s, %(delivery_state)s, %(retry_state)s,
              %(cancellation_state)s, %(supersession_state)s,
              %(prior_state_digest)s, %(content_digest)s, %(payload)s::jsonb
            )
            ON CONFLICT DO NOTHING
            RETURNING state_revision
            """,
            {**payload, "payload": _json(payload)},
            commit=False,
        ).fetchone()
        stored = self._fetchone(
            """
            SELECT payload
            FROM waje_runtime.run_lifecycle_state_revisions
            WHERE run_attempt_id = %(run_attempt_id)s
              AND state_revision = %(state_revision)s
            """,
            payload,
        )
        if stored is None or canonical_value(
            _json_value(_field(stored, "payload", 0)) or {}
        ) != canonical_value(payload):
            raise EvidenceIntegrityError("lifecycle_state_immutable_conflict")
        return "inserted" if inserted is not None else "replayed"

    def runtime_evidence_resolver(self):
        from bi_agent.runtime.runtime_persistence import (
            PostgresRuntimeEvidenceResolver,
        )

        return PostgresRuntimeEvidenceResolver(self.connection)

    def _insert_analysis_authority_graph(
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
    ) -> None:
        from bi_agent.runtime.evidence_authority import canonical_value
        from bi_agent.runtime.runtime_persistence import authority_record_payload

        analysis = canonical_value(analysis_contract)
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
        for contract in query_contracts:
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
        for record in query_execution_records:
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
                    "completeness_report_ref": (record.completeness_report_ref),
                    "payload": _json(canonical_value(record.result_payload)),
                },
                collision="query_run",
            )
        for record in snapshot_records:
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
        for record in rows_records:
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
        for record in query_execution_records:
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
        for record in completeness_records:
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
        for record in capability_binding_records:
            self._insert_authority_record(
                table="capability_binding_authority",
                primary="record_ref",
                columns={
                    "record_ref": record.record_ref,
                    "run_id": run_id,
                    "binding_digest": record.binding_digest,
                    "capability_id": record.capability_id,
                    "analysis_contract_id": record.analysis_contract_ref,
                    "claim_strength_taxonomy_version": (
                        record.claim_strength_taxonomy_version
                    ),
                    "maximum_claim_strength_rank": (record.maximum_claim_strength_rank),
                },
                payload=authority_record_payload("capability_binding", record),
                collision="capability_binding_record",
            )

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
            f"%({name})s::jsonb"
            if name in json_columns or name == "payload"
            else f"%({name})s"
            for name in names
        ]
        where = " AND ".join(
            f"current.{name} = EXCLUDED.{name}" for name in names if name != primary
        )
        params = dict(columns)
        params["payload"] = _json(payload)
        self._insert_immutable(
            f"""
            INSERT INTO waje_runtime.{table} AS current({", ".join(names)})
            VALUES ({", ".join(value_sql)})
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
                  schema_fields, contract_ref, loaded_at, status,
                  logical_snapshot_id, load_revision, evidence_state,
                  reconciliation_status, reconciliation_ref, payload
                ) VALUES (
                  %(snapshot_ref)s, %(dataset_id)s, %(physical_table)s, %(watermark)s,
                  %(schema_fingerprint)s, %(schema_fields)s::jsonb, %(contract_ref)s,
                  %(loaded_at)s, %(status)s,
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
            "payload": _json(payload),
        }
        self._execute(
            """
            INSERT INTO waje_runtime.dataset_snapshots(
              snapshot_ref, dataset_id, physical_table, watermark, schema_fingerprint,
              schema_fields, contract_ref, loaded_at, status,
              logical_snapshot_id, load_revision, evidence_state,
              reconciliation_status, reconciliation_ref, payload
            ) VALUES (
              %(snapshot_ref)s, %(dataset_id)s, %(physical_table)s, %(watermark)s,
              %(schema_fingerprint)s, %(schema_fields)s::jsonb, %(contract_ref)s,
              %(loaded_at)s, %(status)s,
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

    def list_dataset_snapshots(
        self, dataset_id: str = ""
    ) -> tuple[dict[str, Any], ...]:
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
            expected_count = (
                len(canonical_dataset_release_members(stored.dataset_ids[0]))
                if stored is not None
                else 0
            )
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
            immutable_dataset_snapshot_projection(item) for item in member_payloads
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

    def record_run_nodes(
        self, run_id: str, checkpoint_events: tuple[dict, ...]
    ) -> None:
        from bi_agent.conversation.models import canonical_run_checkpoint_events

        normalized_events = canonical_run_checkpoint_events(
            run_id,
            checkpoint_events,
        )
        for index, event in enumerate(normalized_events):
            node_name = event["node"]
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
                    "status": event["status"],
                    "payload": _json(event),
                },
            )
        self._audit(
            "run_nodes_recorded",
            run_id=run_id,
            ref=run_id,
            payload={"count": len(normalized_events)},
        )

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

    def add_memory_item(
        self,
        *,
        owner_id: str,
        text: str,
        source_ref: str,
        status: str,
        refresh_rule: str = "refresh_on_contract_or_owner_change",
        revocation_path: str = "memory_proposal_revoke_or_admin_action",
    ) -> MemoryItem:
        item = MemoryItem(
            memory_id=f"memory-{uuid4().hex[:12]}",
            owner_id=owner_id,
            text=text,
            source_ref=source_ref,
            status=status,
            refresh_rule=refresh_rule,
            revocation_path=revocation_path,
        )
        self._execute(
            """
            INSERT INTO waje_runtime.memory_items(
              memory_id, owner_id, text, source_ref, status,
              ttl, confidence, refresh_rule, revocation_path
            )
            VALUES (
              %(memory_id)s, %(owner_id)s, %(text)s, %(source_ref)s,
              %(status)s, %(ttl)s, %(confidence)s,
              %(refresh_rule)s, %(revocation_path)s
            )
            """,
            item.__dict__,
        )
        self._audit(
            "memory_item_recorded", ref=item.memory_id, payload={"owner_id": owner_id}
        )
        return item

    def long_term_memory(self, owner_id: str) -> tuple[MemoryItem, ...]:
        rows = self._fetchall(
            """
            SELECT memory_id, owner_id, text, source_ref, status,
                   ttl, confidence, refresh_rule, revocation_path
            FROM waje_runtime.memory_items
            WHERE owner_id = %(owner_id)s AND status = 'accepted' AND revoked_at IS NULL
            ORDER BY created_at DESC
            """,
            {"owner_id": owner_id},
        )
        return tuple(
            MemoryItem(
                memory_id=_field(row, "memory_id", 0),
                owner_id=_field(row, "owner_id", 1),
                text=_field(row, "text", 2),
                source_ref=_field(row, "source_ref", 3),
                status=_field(row, "status", 4),
                ttl=_field(row, "ttl", 5),
                confidence=_field(row, "confidence", 6),
                refresh_rule=_field(row, "refresh_rule", 7),
                revocation_path=_field(row, "revocation_path", 8),
            )
            for row in rows
        )

    def add_memory_proposal(self, proposal: MemoryProposal) -> None:
        self._execute(
            """
            INSERT INTO waje_runtime.memory_proposals(
              proposal_id, thread_id, text, source_ref, owner_id, status
            )
            VALUES (
              %(proposal_id)s, %(thread_id)s, %(text)s, %(source_ref)s,
              %(owner_id)s, %(status)s
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
            payload={"owner_id": proposal.owner_id},
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

    def _fetchall(
        self, statement: str, params: Optional[dict[str, Any]] = None
    ) -> list[Any]:
        return list(self.connection.execute(statement, params or {}).fetchall())

    def _audit(
        self,
        event_type: str,
        *,
        actor_id: str | None = None,
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
                "actor_id": actor_id or self._actor_id,
                "thread_id": thread_id,
                "topic_id": topic_id,
                "run_id": run_id,
                "ref": ref,
                "payload": _json(payload or {}),
            },
            commit=commit,
        )


def _plan_result_refs(
    *,
    authority_context: AuthorityContext,
    planner_proposal: PlannerProposal,
    proposal_admission: ProposalAdmissionRecord,
    plan_revision: PlanRevision,
    transition: DurableTransition,
    plan_patch_ref: str | None,
) -> dict[str, Any]:
    from bi_agent.runtime.authoritative_plan_result import (
        AUTHORITATIVE_PLAN_RESULT_SCHEMA_VERSION,
    )

    return {
        "schema_version": AUTHORITATIVE_PLAN_RESULT_SCHEMA_VERSION,
        "plan_patch_ref": plan_patch_ref,
        "intent_revision_id": plan_revision.intent_revision_id,
        "authority_context_ref": authority_context.authority_context_ref,
        "planner_proposal_id": planner_proposal.planner_proposal_id,
        "proposal_admission_id": proposal_admission.proposal_admission_id,
        "plan_revision_id": plan_revision.plan_revision_id,
        "accepted_transition_id": transition.transition_id,
    }


def _plan_result_refs_from_revision(
    *,
    plan_revision: PlanRevision,
    accepted_transition_id: str,
    plan_patch_ref: str | None,
) -> dict[str, Any]:
    from bi_agent.runtime.authoritative_plan_result import (
        AUTHORITATIVE_PLAN_RESULT_SCHEMA_VERSION,
    )

    return {
        "schema_version": AUTHORITATIVE_PLAN_RESULT_SCHEMA_VERSION,
        "plan_patch_ref": plan_patch_ref,
        "intent_revision_id": plan_revision.intent_revision_id,
        "authority_context_ref": plan_revision.authority_context_ref,
        "planner_proposal_id": plan_revision.planner_proposal_ref,
        "proposal_admission_id": plan_revision.proposal_admission_ref,
        "plan_revision_id": plan_revision.plan_revision_id,
        "accepted_transition_id": accepted_transition_id,
    }


def _execution_result_refs(result: Any) -> dict[str, str]:
    return {
        "schema_version": result.schema_version,
        "authoritative_execution_result_ref": (
            result.authoritative_execution_result_ref
        ),
        "intent_revision_id": result.intent_revision_id,
        "authority_context_ref": result.authority_context_ref,
        "plan_revision_id": result.plan_revision_id,
        "execution_snapshot_ref": result.execution_snapshot_ref,
        "stop_ref": result.stop_ref,
        "accepted_transition_id": result.transition_id,
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _topic_row_matches(row: Any, topic: TopicState) -> bool:
    from bi_agent.runtime.evidence_authority import canonical_value

    return (
        str(_field(row, "thread_id", 0) or "") == topic.thread_id
        and str(_field(row, "title", 1) or "") == topic.title
        and str(_field(row, "summary", 2) or "") == topic.summary
        and str(_field(row, "status", 3) or "") == topic.status
        and canonical_value(_json_value(_field(row, "assumptions", 4)) or [])
        == canonical_value(topic.assumptions)
        and canonical_value(_json_value(_field(row, "open_questions", 5)) or [])
        == canonical_value(topic.open_questions)
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _run_dispatch_lease_ms() -> int:
    raw = os.environ.get("WAJE_RUN_DISPATCH_LEASE_MS", "30000")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("run_dispatch_lease_configuration_invalid") from exc
    if str(value) != raw or value < 1 or value > 86_400_000:
        raise RuntimeError("run_dispatch_lease_configuration_invalid")
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
        run_id=payload.get("run_id"),
        items=tuple(
            ContextItem(
                source_type=item.get("source_type") or item.get("type", ""),
                source_ref=item.get("source_ref") or item.get("ref", ""),
                summary=item.get("summary", ""),
                can_support_claims=bool(item.get("can_support_claims")),
                reason=item.get("reason", ""),
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
        accepted_assumptions=list(payload.get("accepted_assumptions") or []),
        contract_versions=dict(payload.get("contract_versions") or {}),
        schema_fingerprint=str(payload.get("schema_fingerprint") or ""),
        created_at=payload.get("created_at"),
        can_support_claims=bool(
            payload.get("can_support_claims", _field(row, "can_support_claims", 3))
        ),
    )
