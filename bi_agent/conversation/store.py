from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from threading import RLock
from typing import Any, Optional
from uuid import uuid4

from bi_agent.conversation.models import (
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
from bi_agent.runtime.durable_call_journal import InMemoryDurableCallJournal
from bi_agent.runtime.dataset_catalog import (
    DatasetReleaseAuthorityRecord,
    build_dataset_release_authority_record,
    canonical_dataset_release_members,
    canonical_dataset_requires_release,
    dataset_release_authority_record_from_mapping,
    immutable_dataset_snapshot_projection,
    validate_dataset_snapshot_release_payloads,
)


class InMemoryConversationStore:
    def __init__(self) -> None:
        self.threads: dict[str, ThreadState] = {}
        self.topics: dict[str, TopicState] = {}
        self.thread_topics: dict[str, list[str]] = defaultdict(list)
        self.memory_items: dict[str, list[MemoryItem]] = defaultdict(list)
        self.memory_proposals: dict[str, MemoryProposal] = {}
        self.runs: dict[str, dict] = {}
        self.context_manifests: dict[str, dict] = {}
        self.conversation_entry_commands: dict[str, dict[str, Any]] = {}
        self.conversation_entry_transitions: dict[str, dict[str, Any]] = {}
        self.post_seal_failure_terminals: dict[str, Any] = {}
        self.post_seal_failure_history: dict[str, list[Any]] = defaultdict(list)
        self.attempt_journal = InMemoryDurableCallJournal()
        self._conversation_entry_locks_guard = RLock()
        self._conversation_entry_locks: dict[str, RLock] = {}
        self.dataset_snapshots: dict[str, dict[str, Any]] = {}
        self.dataset_snapshot_releases: dict[str, dict[str, Any]] = {}
        self.clarification_states: dict[str, ClarificationState] = {}
        self._audit_events: list[dict] = []
        self._actor_id = "system"

    def set_actor_id(self, actor_id: str) -> None:
        self._actor_id = actor_id or "system"

    @property
    def audit_events(self) -> list[dict]:
        return deepcopy(self._audit_events)

    def recover_after_write_failure(self) -> None:
        return None

    def load_post_seal_failure_terminal(
        self,
        *,
        authority_bundle: Any,
        authority_transition: Any,
    ) -> Any:
        terminal = self.post_seal_failure_terminals.get(authority_bundle.run_attempt_id)
        if terminal is None:
            return None
        from bi_agent.runtime.post_seal_failure_persistence import (
            PostSealFailureTerminal,
        )

        return PostSealFailureTerminal.from_dict(
            terminal.to_dict(),
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
        failure_record: Any,
        supersedes_terminal_ref: str | None,
    ) -> Any:
        from bi_agent.runtime.post_seal_failure_persistence import (
            PostSealFailurePersistenceError,
            PostSealFailurePersistenceResult,
            PostSealFailureTerminal,
        )
        from bi_agent.runtime.single_authority import LifecycleState

        existing = self.post_seal_failure_terminals.get(authority_bundle.run_attempt_id)
        if existing is not None:
            if supersedes_terminal_ref is None:
                expected = PostSealFailureTerminal.create(
                    attempt_number=1,
                    supersedes_terminal_ref=None,
                    status=status,
                    authority_bundle=authority_bundle,
                    authority_transition=authority_transition,
                    failure_record=failure_record,
                    lifecycle_state=existing.lifecycle_state,
                )
                if expected != existing:
                    raise PostSealFailurePersistenceError(
                        "post_seal_failure_terminal_replay_conflict"
                    )
                return PostSealFailurePersistenceResult(
                    terminal=existing,
                    status="replayed",
                )
            if supersedes_terminal_ref != existing.terminal_ref:
                if existing.supersedes_terminal_ref != supersedes_terminal_ref:
                    raise PostSealFailurePersistenceError(
                        "post_seal_failure_retry_cas_conflict"
                    )
                expected = PostSealFailureTerminal.create(
                    attempt_number=existing.attempt_number,
                    supersedes_terminal_ref=supersedes_terminal_ref,
                    status=status,
                    authority_bundle=authority_bundle,
                    authority_transition=authority_transition,
                    failure_record=failure_record,
                    lifecycle_state=existing.lifecycle_state,
                )
                if expected != existing:
                    raise PostSealFailurePersistenceError(
                        "post_seal_failure_terminal_replay_conflict"
                    )
                return PostSealFailurePersistenceResult(
                    terminal=existing,
                    status="replayed",
                )
            if existing.failure_record.retryability != "retryable":
                raise PostSealFailurePersistenceError("post_seal_failure_not_retryable")
            lifecycle = existing.lifecycle_state.transition(retry_state="exhausted")
            attempt_number = existing.attempt_number + 1
        else:
            if supersedes_terminal_ref is not None:
                raise PostSealFailurePersistenceError(
                    "post_seal_failure_retry_cas_conflict"
                )
            lifecycle = LifecycleState.create(
                run_attempt_id=authority_bundle.run_attempt_id,
                execution_state="complete",
                evidence_state=(
                    "boundary_only"
                    if authority_bundle.authority_mode == "boundary_only"
                    else "complete"
                ),
                retry_state="exhausted",
            )
            attempt_number = 1
        terminal = PostSealFailureTerminal.create(
            attempt_number=attempt_number,
            supersedes_terminal_ref=supersedes_terminal_ref,
            status=status,
            authority_bundle=authority_bundle,
            authority_transition=authority_transition,
            failure_record=failure_record,
            lifecycle_state=lifecycle,
        )
        self.post_seal_failure_terminals[authority_bundle.run_attempt_id] = terminal
        self.post_seal_failure_history[authority_bundle.run_attempt_id].append(terminal)
        self._audit_events.append(
            {
                "event_type": "post_seal_failure_recorded",
                "actor_id": owner_ref,
                "thread_id": thread_ref,
                "run_id": authority_bundle.run_attempt_id,
                "ref": terminal.terminal_ref,
                "payload": terminal.to_dict(),
            }
        )
        return PostSealFailurePersistenceResult(
            terminal=terminal,
            status="inserted",
        )

    def create_thread(
        self, thread_id: Optional[str] = None, *, owner_id: str = "user"
    ) -> ThreadState:
        thread_id = thread_id or f"thread-{uuid4().hex[:12]}"
        thread = ThreadState(thread_id=thread_id, owner_id=owner_id)
        self.threads[thread_id] = thread
        return thread

    def get_thread(self, thread_id: str) -> ThreadState:
        if thread_id not in self.threads:
            return self.create_thread(thread_id)
        return self.threads[thread_id]

    def create_topic(
        self, thread_id: str, *, title: str, summary: str = ""
    ) -> TopicState:
        self.get_thread(thread_id)
        topic_id = f"topic-{uuid4().hex[:12]}"
        topic = TopicState(
            topic_id=topic_id,
            thread_id=thread_id,
            title=title,
            summary=summary or title,
        )
        self.topics[topic_id] = topic
        self.thread_topics[thread_id].append(topic_id)
        if not self.threads[thread_id].current_topic_id:
            self.threads[thread_id].current_topic_id = topic_id
        return topic

    def topic(self, topic_id: Optional[str]) -> Optional[TopicState]:
        if not topic_id:
            return None
        return self.topics.get(topic_id)

    def topics_for_thread(self, thread_id: str) -> tuple[TopicState, ...]:
        return tuple(
            self.topics[topic_id]
            for topic_id in self.thread_topics.get(thread_id, [])
            if topic_id in self.topics
        )

    def current_topic(self, thread_id: str) -> Optional[TopicState]:
        thread = self.get_thread(thread_id)
        return self.topic(thread.current_topic_id)

    def set_current_topic(self, thread_id: str, topic_id: str) -> None:
        self.get_thread(thread_id).current_topic_id = topic_id

    @contextmanager
    def conversation_entry_lock(self, run_attempt_id: str):
        if not isinstance(run_attempt_id, str) or not run_attempt_id.strip():
            raise ValueError("conversation_entry_run_attempt_id_invalid")
        with self._conversation_entry_locks_guard:
            lock = self._conversation_entry_locks.setdefault(run_attempt_id, RLock())
        with lock:
            yield

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

        if thread_id not in self.threads:
            raise EvidenceIntegrityError("conversation_entry_thread_missing")
        run = self.runs.get(run_attempt_id)
        if run is not None and str(run.get("thread_id") or "") != thread_id:
            raise EvidenceIntegrityError("conversation_entry_run_owner_mismatch")
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
        existing = self.conversation_entry_commands.get(run_attempt_id)
        if existing is not None:
            if canonical_value(existing.get("command_payload")) != command:
                raise EvidenceIntegrityError("conversation_entry_command_conflict")
            return deepcopy(existing)
        self.conversation_entry_commands[run_attempt_id] = deepcopy(state)
        if run is not None:
            run["request"] = canonical_value(
                {
                    **dict(run.get("request") or {}),
                    "conversation_entry": state,
                }
            )
        return deepcopy(state)

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
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )
        from bi_agent.conversation.entry_authority import (
            CONVERSATION_ENTRY_STAGE,
            build_conversation_entry_transition,
        )
        from bi_agent.runtime.durable_call_journal import DurableCallJournalError
        from bi_agent.runtime.single_authority import DurableTransition

        try:
            accepted_call = self.attempt_journal.load_accepted_call(
                call_spec=call_spec,
                accepted_attempt_ref=accepted_attempt_ref,
            )
        except DurableCallJournalError as exc:
            raise EvidenceIntegrityError(
                "conversation_entry_acceptance_invalid"
            ) from exc
        stored_command = self.conversation_entry_commands.get(run_attempt_id)
        if (
            stored_command is None
            or canonical_value(stored_command) != canonical_value(command_state)
            or not isinstance(accepted_attempt_ref, str)
            or not accepted_attempt_ref.strip()
        ):
            raise EvidenceIntegrityError("conversation_entry_acceptance_invalid")
        thread_id = str(command_state.get("thread_id") or "")
        thread = self.threads.get(thread_id)
        if thread is None:
            raise EvidenceIntegrityError("conversation_entry_thread_missing")
        turn_payload = canonical_value(turn)
        manifest_payload = canonical_value(manifest.to_dict())
        turn_id = str(turn_payload.get("turn_id") or "")
        manifest_id = str(manifest_payload.get("manifest_id") or "")
        if (
            not turn_id
            or not manifest_id
            or str(turn_payload.get("thread_id") or "") != thread_id
            or str(manifest_payload.get("thread_id") or "") != thread_id
            or str(manifest_payload.get("run_id") or "") != run_attempt_id
        ):
            raise EvidenceIntegrityError("conversation_entry_binding_invalid")

        existing_turns = [
            item for item in thread.turns if str(item.get("turn_id") or "") == turn_id
        ]
        existing_manifest = self.context_manifests.get(manifest_id)
        run = self.runs.get(run_attempt_id)
        topic_id = topic.topic_id if topic is not None else ""
        try:
            supplied_transition = DurableTransition.from_dict(transition.to_dict())
            expected_transition, transition_input, transition_output = (
                build_conversation_entry_transition(
                    run_attempt_id=run_attempt_id,
                    command_state=command_state,
                    call_spec=accepted_call.attempt.spec,
                    accepted_attempt_ref=accepted_attempt_ref,
                    accepted_output_payload=accepted_call.acceptance.output_payload,
                    orchestration=orchestration,
                    topic=topic,
                    topic_is_new=topic_is_new,
                    set_current_topic=set_current_topic,
                    turn=turn_payload,
                    manifest=manifest,
                )
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvidenceIntegrityError(
                "conversation_entry_transition_invalid"
            ) from exc
        if supplied_transition != expected_transition:
            raise EvidenceIntegrityError("conversation_entry_transition_invalid")
        transition_record = canonical_value(
            {
                "transition": expected_transition.to_dict(),
                "input_payload": transition_input,
                "output_payload": transition_output,
            }
        )
        stored_transition = self.conversation_entry_transitions.get(run_attempt_id)
        run_entry = (
            (run.get("request") or {}).get("conversation_entry")
            if isinstance(run, Mapping) and isinstance(run.get("request"), Mapping)
            else None
        )
        if existing_turns or existing_manifest is not None:
            if (
                len(existing_turns) == 1
                and canonical_value(existing_turns[0]) == turn_payload
                and canonical_value(existing_manifest) == manifest_payload
                and canonical_value(stored_transition) == transition_record
                and (
                    run is None
                    or (
                        str(run.get("turn_id") or "") == turn_id
                        and str(run.get("topic_id") or "") == topic_id
                        and isinstance(run_entry, Mapping)
                        and run_entry.get("accepted_attempt_ref")
                        == accepted_attempt_ref
                        and run_entry.get("transition_attempt_id")
                        == expected_transition.attempt_id
                    )
                )
            ):
                if self.attempt_journal.load_stage_attempt_refs(
                    run_attempt_id=run_attempt_id,
                    transition_attempt_id=expected_transition.attempt_id,
                    stage_name=CONVERSATION_ENTRY_STAGE,
                ) != (accepted_attempt_ref,):
                    raise EvidenceIntegrityError("conversation_entry_binding_conflict")
                return "replayed"
            raise EvidenceIntegrityError("conversation_entry_binding_conflict")
        if isinstance(run_entry, Mapping) and run_entry.get("accepted_attempt_ref"):
            raise EvidenceIntegrityError("conversation_entry_binding_conflict")

        staged_threads = deepcopy(self.threads)
        staged_topics = deepcopy(self.topics)
        staged_thread_topics = deepcopy(self.thread_topics)
        staged_manifests = deepcopy(self.context_manifests)
        staged_transitions = deepcopy(self.conversation_entry_transitions)
        staged_runs = deepcopy(self.runs)
        staged_events = deepcopy(self._audit_events)
        staged_thread = staged_threads[thread_id]
        if topic is not None:
            existing_topic = staged_topics.get(topic.topic_id)
            if topic_is_new:
                if existing_topic is not None and existing_topic != topic:
                    raise EvidenceIntegrityError("conversation_entry_topic_conflict")
                if existing_topic is None:
                    staged_topics[topic.topic_id] = topic
                    staged_thread_topics[thread_id].append(topic.topic_id)
            elif existing_topic != topic or topic.thread_id != thread_id:
                raise EvidenceIntegrityError("conversation_entry_topic_invalid")
            if set_current_topic:
                staged_thread.current_topic_id = topic.topic_id
        elif set_current_topic:
            raise EvidenceIntegrityError("conversation_entry_topic_invalid")
        self._conversation_entry_failpoint("after_topic")
        staged_thread.turns.append(deepcopy(turn_payload))
        self._conversation_entry_failpoint("after_turn")
        staged_manifests[manifest_id] = deepcopy(manifest_payload)
        self._conversation_entry_failpoint("after_manifest")
        staged_transitions[run_attempt_id] = deepcopy(transition_record)
        self._conversation_entry_failpoint("after_transition")
        run = staged_runs.get(run_attempt_id)
        if run is not None:
            existing_turn_id = str(run.get("turn_id") or "")
            existing_topic_id = str(run.get("topic_id") or "")
            if existing_turn_id not in {"", turn_id} or existing_topic_id not in {
                "",
                topic_id,
            }:
                raise EvidenceIntegrityError("conversation_entry_run_binding_conflict")
            run.update(
                {
                    "turn_id": turn_id,
                    "topic_id": topic_id,
                    "request": canonical_value(
                        {
                            **dict(run.get("request") or {}),
                            "conversation_entry": {
                                **dict(command_state),
                                "accepted_attempt_ref": accepted_attempt_ref,
                                "accepted_output_digest": (
                                    accepted_call.acceptance.output_digest
                                ),
                                "transition_attempt_id": (
                                    expected_transition.attempt_id
                                ),
                            },
                        }
                    ),
                }
            )
        self._conversation_entry_failpoint("before_commit")
        self.attempt_journal.bind_stage(
            run_attempt_id=run_attempt_id,
            transition_attempt_id=expected_transition.attempt_id,
            stage_name=CONVERSATION_ENTRY_STAGE,
            attempt_refs=(accepted_attempt_ref,),
        )
        self.threads = staged_threads
        self.topics = staged_topics
        self.thread_topics = staged_thread_topics
        self.context_manifests = staged_manifests
        self.conversation_entry_transitions = staged_transitions
        self.runs = staged_runs
        self._audit_events = staged_events
        return "accepted"

    def _conversation_entry_failpoint(self, stage: str) -> None:
        del stage

    def active_conversation_run_status(
        self,
        thread_id: str,
        *,
        exclude_run_id: str,
    ) -> str:
        return (
            "running"
            if any(
                str(run_id) != exclude_run_id
                and str(run.get("thread_id") or "") == thread_id
                and str(run.get("status") or "") in {"queued", "running"}
                for run_id, run in self.runs.items()
            )
            else "idle"
        )

    def set_pending_clarification(
        self, thread_id: str, topic_id: str, clarification_id: str
    ) -> None:
        thread = self.get_thread(thread_id)
        thread.pending_clarification_topic_id = topic_id
        thread.pending_clarification_id = clarification_id

    def clear_pending_clarification(self, thread_id: str) -> None:
        thread = self.get_thread(thread_id)
        thread.pending_clarification_topic_id = None
        thread.pending_clarification_id = ""

    def save_clarification_state(self, state: ClarificationState) -> None:
        self.clarification_states[state.run_id] = state

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
        from bi_agent.runtime.evidence_authority import EvidenceIntegrityError

        if (
            clarification_state.run_id != run_id
            or clarification_state.topic_id != topic_id
            or clarification_state.status != "waiting"
        ):
            raise EvidenceIntegrityError("waiting_clarification_state_owner_mismatch")
        run = self.runs.get(run_id)
        thread = self.threads.get(thread_id)
        topic = self.topics.get(topic_id)
        if (
            not isinstance(run, Mapping)
            or thread is None
            or topic is None
            or topic.thread_id != thread_id
        ):
            raise EvidenceIntegrityError("waiting_clarification_source_missing")
        action = validate_run_status_transition(
            current_status=str(run.get("status") or ""),
            next_status="waiting_for_clarification",
            current_thread_id=str(run.get("thread_id") or ""),
            current_turn_id=str(run.get("turn_id") or ""),
            current_topic_id=str(run.get("topic_id") or ""),
            next_thread_id=thread_id,
            next_turn_id=turn_id,
            next_topic_id=topic_id,
            current_request=run.get("request") or {},
            next_request=dict(request),
        )
        if action == "replay":
            existing_state = self.clarification_states.get(run_id)
            if (
                thread.pending_clarification_id == run_id
                and thread.pending_clarification_topic_id == topic_id
                and existing_state is not None
                and existing_state.to_dict() == clarification_state.to_dict()
            ):
                return "replayed"
            raise EvidenceIntegrityError("waiting_clarification_replay_conflict")

        staged_runs = deepcopy(self.runs)
        staged_threads = deepcopy(self.threads)
        staged_states = deepcopy(self.clarification_states)
        staged_events = deepcopy(self._audit_events)
        staged_thread = staged_threads[thread_id]
        staged_thread.pending_clarification_topic_id = topic_id
        staged_thread.pending_clarification_id = run_id
        staged_states[run_id] = deepcopy(clarification_state)
        staged_runs[run_id] = {
            **dict(run),
            "thread_id": thread_id,
            "turn_id": turn_id,
            "topic_id": topic_id,
            "status": "waiting_for_clarification",
            "request": deepcopy(dict(request)),
        }
        for event in (
            {
                "event_type": "run_status_changed",
                "thread_id": thread_id,
                "topic_id": topic_id,
                "run_id": run_id,
                "ref": run_id,
                "payload": {"status": "waiting_for_clarification"},
            },
            {
                "event_type": "clarification_pending",
                "thread_id": thread_id,
                "topic_id": topic_id,
                "run_id": run_id,
                "ref": run_id,
                "payload": {},
            },
            {
                "event_type": "clarification_state_saved",
                "thread_id": thread_id,
                "topic_id": topic_id,
                "run_id": run_id,
                "ref": run_id,
                "payload": clarification_state.to_dict(),
            },
        ):
            self._append_staged_audit_event(staged_events, event)
        self.runs = staged_runs
        self.threads = staged_threads
        self.clarification_states = staged_states
        self._audit_events = staged_events
        return "inserted"

    def get_open_clarification(self, thread_id: str) -> Optional[ClarificationState]:
        topic_ids = set(self.thread_topics.get(thread_id, []))
        for state in reversed(tuple(self.clarification_states.values())):
            if state.status == "waiting" and state.topic_id in topic_ids:
                return state
        return None

    def upsert_run(
        self,
        run_id: str,
        *,
        thread_id: str,
        turn_id: str = "",
        topic_id: str = "",
        status: str,
        request: dict | None = None,
    ) -> None:
        validate_run_status_value(status)
        staged_runs = dict(self.runs)
        staged_events = deepcopy(self._audit_events)
        existing = staged_runs.get(run_id)
        if existing:
            action = validate_run_status_transition(
                current_status=str(existing.get("status") or ""),
                next_status=status,
                current_thread_id=str(existing.get("thread_id") or ""),
                current_turn_id=str(existing.get("turn_id") or ""),
                current_topic_id=str(existing.get("topic_id") or ""),
                next_thread_id=thread_id,
                next_turn_id=turn_id,
                next_topic_id=topic_id,
                current_request=existing.get("request") or {},
                next_request=request or {},
            )
            if action == "replay":
                return
        staged_runs[run_id] = {
            "run_id": run_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "topic_id": topic_id,
            "status": status,
            "request": deepcopy(request or {}),
            "checkpoint_events": staged_runs.get(run_id, {}).get(
                "checkpoint_events", []
            ),
        }
        self._append_staged_audit_event(
            staged_events,
            {
                "event_type": "run_status_changed",
                "thread_id": thread_id,
                "topic_id": topic_id,
                "run_id": run_id,
                "ref": "",
                "payload": {},
            },
        )
        self.runs = staged_runs
        self._audit_events = staged_events

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

        run = self.runs.get(run_id)
        if not isinstance(run, Mapping):
            raise EvidenceIntegrityError("analysis_run_failure_source_missing")
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
        action = validate_run_status_transition(
            current_status=str(run.get("status") or ""),
            next_status="failed",
            current_thread_id=str(run.get("thread_id") or ""),
            current_turn_id=str(run.get("turn_id") or ""),
            current_topic_id=str(run.get("topic_id") or ""),
            next_thread_id=thread_id,
            next_turn_id=turn_id,
            next_topic_id=topic_id,
            current_request=run.get("request") or {},
            next_request=finalized_request,
        )
        existing_primary = tuple(
            event
            for event in self._audit_events
            if event.get("event_type") == failure_reason
            and event.get("run_id") == run_id
        )
        if action == "replay":
            if (
                len(existing_primary) == 1
                and str(existing_primary[0].get("thread_id") or "") == thread_id
                and str(existing_primary[0].get("topic_id") or "") == topic_id
                and str(existing_primary[0].get("ref") or "") == run_id
                and canonical_value(existing_primary[0].get("payload") or {})
                == primary_payload
            ):
                return deepcopy(finalized_request)
            raise EvidenceIntegrityError("analysis_run_failure_record_conflict")
        if existing_primary:
            raise EvidenceIntegrityError("analysis_run_failure_record_conflict")

        staged_runs = deepcopy(self.runs)
        staged_events = deepcopy(self._audit_events)
        staged_runs[run_id] = {
            **dict(run),
            "thread_id": thread_id,
            "turn_id": turn_id,
            "topic_id": topic_id,
            "status": "failed",
            "request": finalized_request,
        }
        self._append_staged_audit_event(
            staged_events,
            {
                "event_type": "run_status_changed",
                "thread_id": thread_id,
                "topic_id": topic_id,
                "run_id": run_id,
                "ref": run_id,
                "payload": {"status": "failed"},
            },
        )
        self._append_staged_audit_event(
            staged_events,
            {
                "event_type": failure_reason,
                "thread_id": thread_id,
                "topic_id": topic_id,
                "run_id": run_id,
                "ref": run_id,
                "payload": primary_payload,
            },
        )
        self.runs = staged_runs
        self._audit_events = staged_events
        return deepcopy(finalized_request)

    def get_run_request(self, run_id: str) -> dict[str, Any]:
        run = self.runs.get(run_id) or {}
        request = deepcopy(run.get("request") or {})
        request["thread_id"] = str(run.get("thread_id") or "")
        request["topic_id"] = str(run.get("topic_id") or "")
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

        run = self.runs.get(run_id)
        if (
            not isinstance(run, Mapping)
            or str(run.get("status") or "") != "completed"
            or str(run.get("thread_id") or "") != thread_id
            or str(run.get("turn_id") or "") != turn_id
            or str(run.get("topic_id") or "") != topic_id
        ):
            raise EvidenceIntegrityError("terminal_completion_conflict_owner_unproven")
        conflict_payload = canonical_value(
            {**dict(payload), "durable_run_status": "completed"}
        )
        existing = tuple(
            event
            for event in self._audit_events
            if event.get("event_type") == failure_reason
            and event.get("run_id") == run_id
        )
        if existing:
            if (
                len(existing) == 1
                and str(existing[0].get("thread_id") or "") == thread_id
                and str(existing[0].get("topic_id") or "") == topic_id
                and str(existing[0].get("ref") or "") == run_id
                and canonical_value(existing[0].get("payload") or {})
                == conflict_payload
            ):
                return deepcopy(conflict_payload)
            raise EvidenceIntegrityError("terminal_completion_conflict_audit_mismatch")
        staged_events = deepcopy(self._audit_events)
        self._append_staged_audit_event(
            staged_events,
            {
                "event_type": failure_reason,
                "thread_id": thread_id,
                "topic_id": topic_id,
                "run_id": run_id,
                "ref": run_id,
                "payload": conflict_payload,
            },
        )
        self._audit_events = staged_events
        return deepcopy(conflict_payload)

    def get_run_state(self, run_id: str) -> dict[str, Any] | None:
        run = self.runs.get(run_id)
        if not isinstance(run, Mapping):
            return None
        return deepcopy(
            {
                "run_id": str(run.get("run_id") or ""),
                "thread_id": str(run.get("thread_id") or ""),
                "turn_id": str(run.get("turn_id") or ""),
                "topic_id": str(run.get("topic_id") or ""),
                "status": str(run.get("status") or ""),
                "request": run.get("request") or {},
            }
        )

    def record_context_manifest(self, manifest: dict) -> None:
        self.save_context_manifest(manifest)

    def save_context_manifest(self, manifest: ContextManifest | dict) -> None:
        from bi_agent.runtime.context_manifest import (
            validated_context_manifest_record,
        )
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        payload = manifest.to_dict() if hasattr(manifest, "to_dict") else dict(manifest)
        if "manifest_digest" in payload:
            payload = validated_context_manifest_record(payload)
        payload = canonical_value(payload)
        manifest_id = str(payload["manifest_id"])
        existing = self.context_manifests.get(manifest_id)
        if existing is not None and canonical_value(existing) != payload:
            raise EvidenceIntegrityError("context_manifest_publication_conflict")
        if existing is not None:
            return
        self.context_manifests[manifest_id] = deepcopy(payload)
        self.add_audit_event(
            "context_manifest_recorded",
            thread_id=payload.get("thread_id", ""),
            ref=manifest_id,
        )

    def list_context_manifests(self, thread_id: str) -> tuple[ContextManifest, ...]:
        return tuple(
            _context_manifest_from_payload(payload)
            for payload in self.context_manifests.values()
            if payload.get("thread_id") == thread_id
        )

    def save_dataset_snapshot(self, payload: dict[str, Any]) -> None:
        dataset_id = str(payload.get("dataset_id") or "")
        snapshot_ref = str(payload.get("snapshot_ref") or "")
        if any(
            snapshot_ref in tuple(release.get("snapshot_refs") or ())
            for release in self.dataset_snapshot_releases.values()
        ):
            raise ValueError("dataset_snapshot_published_immutable")
        if (
            canonical_dataset_requires_release(dataset_id)
            and payload.get("status") == "active"
        ):
            raise ValueError("dataset_snapshot_release_required")
        snapshot = deepcopy(payload)
        self.dataset_snapshots[snapshot["snapshot_ref"]] = snapshot
        self.add_audit_event(
            "dataset_snapshot_saved",
            ref=snapshot["snapshot_ref"],
            payload=deepcopy(snapshot),
        )

    def list_dataset_snapshots(
        self, dataset_id: str = ""
    ) -> tuple[dict[str, Any], ...]:
        snapshots = []
        for payload in self.dataset_snapshots.values():
            if dataset_id and payload.get("dataset_id") != dataset_id:
                continue
            snapshots.append(deepcopy(payload))
        return tuple(snapshots)

    def publish_dataset_snapshot_release(
        self,
        *,
        release_ref: str,
        logical_snapshot_id: str,
        payloads: tuple[dict[str, Any], ...],
        fail_after_writes: int = 0,
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
        for payload in payloads:
            existing = self.dataset_snapshots.get(str(payload["snapshot_ref"]))
            if existing is None:
                continue
            if existing.get("status") != payload.get(
                "status"
            ) or immutable_dataset_snapshot_projection(
                existing
            ) != immutable_dataset_snapshot_projection(payload):
                raise ValueError("dataset_snapshot_published_immutable")
        snapshots_before = deepcopy(self.dataset_snapshots)
        releases_before = deepcopy(self.dataset_snapshot_releases)
        audit_before = deepcopy(self._audit_events)
        writes = 0
        try:
            new_refs = {str(payload["snapshot_ref"]) for payload in payloads}
            for ref, existing in tuple(self.dataset_snapshots.items()):
                if (
                    existing.get("logical_snapshot_id") == logical_snapshot_id
                    and existing.get("status") == "active"
                    and ref not in new_refs
                ):
                    self.dataset_snapshots[ref] = {
                        **existing,
                        "status": "superseded",
                        "superseded_by_release": release_ref,
                    }
            for payload in payloads:
                self.dataset_snapshots[str(payload["snapshot_ref"])] = deepcopy(payload)
                writes += 1
                if fail_after_writes and writes >= fail_after_writes:
                    raise RuntimeError("injected_release_failure")
            release = authority.to_dict()
            self.dataset_snapshot_releases[release_ref] = release
            self.add_audit_event(
                "dataset_snapshot_release_published",
                ref=release_ref,
                payload=release,
            )
        except Exception:
            self.dataset_snapshots = snapshots_before
            self.dataset_snapshot_releases = releases_before
            self._audit_events = audit_before
            raise

    def resolve_dataset_release(
        self,
        release_ref: str,
    ) -> DatasetReleaseAuthorityRecord:
        release = self.dataset_snapshot_releases.get(release_ref)
        if not release:
            raise KeyError(f"dataset_release_unavailable:{release_ref}")
        snapshot_refs = tuple(release.get("snapshot_refs") or ())
        try:
            expected_count = len(
                canonical_dataset_release_members(
                    str(
                        release.get("member_projections", [{}])[0].get("dataset_id")
                        or ""
                    )
                )
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("dataset_release_authority_membership") from exc
        if len(snapshot_refs) != expected_count or any(
            ref not in self.dataset_snapshots for ref in snapshot_refs
        ):
            raise ValueError("dataset_release_authority_membership")
        stored = dataset_release_authority_record_from_mapping(release)
        if stored.integrity_errors or stored.release_ref != release_ref:
            raise ValueError("dataset_release_authority_record_mismatch")
        current = tuple(
            immutable_dataset_snapshot_projection(self.dataset_snapshots[ref])
            for ref in snapshot_refs
        )
        if current != stored.member_projections:
            raise ValueError("dataset_release_authority_record_mismatch")
        return stored

    def record_run_nodes(
        self, run_id: str, checkpoint_events: tuple[dict, ...]
    ) -> None:
        from bi_agent.conversation.models import canonical_run_checkpoint_events

        if run_id not in self.runs:
            raise ValueError("run_checkpoint_owner_missing")
        normalized_events = canonical_run_checkpoint_events(
            run_id,
            checkpoint_events,
        )
        self.runs[run_id]["checkpoint_events"] = list(normalized_events)
        self.add_audit_event("run_nodes_recorded", run_id=run_id, ref=run_id)

    def add_audit_event(
        self,
        event_type: str,
        *,
        thread_id: str = "",
        topic_id: str = "",
        run_id: str = "",
        ref: str = "",
        payload: dict | None = None,
    ) -> None:
        self._append_staged_audit_event(
            self._audit_events,
            {
                "event_type": event_type,
                "actor_id": self._actor_id,
                "thread_id": thread_id,
                "topic_id": topic_id,
                "run_id": run_id,
                "ref": ref,
                "payload": payload or {},
            },
        )

    def _append_staged_audit_event(
        self,
        events: list[dict],
        event: Mapping[str, Any],
    ) -> None:
        events.append(deepcopy(dict(event)))

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
        self.memory_items[owner_id].append(item)
        return item

    def long_term_memory(self, owner_id: str) -> tuple[MemoryItem, ...]:
        return tuple(
            item
            for item in self.memory_items.get(owner_id, ())
            if item.status == "accepted"
        )

    def add_memory_proposal(self, proposal: MemoryProposal) -> None:
        self.memory_proposals[proposal.proposal_id] = proposal

    def accept_memory_proposal(self, proposal_id: str) -> Optional[MemoryItem]:
        proposal = self.memory_proposals.get(proposal_id)
        if not proposal:
            return None
        return self.add_memory_item(
            owner_id=proposal.owner_id,
            text=proposal.text,
            source_ref=proposal.source_ref,
            status="accepted",
        )


def _context_manifest_from_payload(payload: dict) -> ContextManifest:
    return ContextManifest(
        manifest_id=payload["manifest_id"],
        thread_id=payload["thread_id"],
        turn_id=payload.get("turn_id", ""),
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
        can_support_claims=bool(payload.get("can_support_claims")),
    )
