from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any, Optional
from uuid import uuid4

from bi_agent.conversation.models import (
    ArtifactRef,
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
from bi_agent.conversation.run_status import (
    validate_run_status_transition,
    validate_run_status_value,
)
from bi_agent.runtime.analysis_assets import merge_analysis_assets
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
        self.result_refs: dict[str, list[ResultRefRecord]] = defaultdict(list)
        self.artifacts: dict[str, ArtifactRef] = {}
        self.memory_items: dict[str, list[MemoryItem]] = defaultdict(list)
        self.memory_proposals: dict[str, MemoryProposal] = {}
        self.runs: dict[str, dict] = {}
        self.context_manifests: dict[str, dict] = {}
        self.reuse_decisions: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.answer_packages: dict[str, dict] = {}
        self.analysis_assets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.dataset_snapshots: dict[str, dict[str, Any]] = {}
        self.dataset_snapshot_releases: dict[str, dict[str, Any]] = {}
        self.clarification_states: dict[str, ClarificationState] = {}
        self.analysis_runtime_records: dict[str, dict[str, Any]] = {}
        self.analysis_runtime_authority: dict[str, dict[str, Any]] = defaultdict(dict)
        self._audit_events: list[dict] = []

    @property
    def audit_events(self) -> list[dict]:
        return deepcopy(self._audit_events)

    def recover_after_write_failure(self) -> None:
        return None

    def create_thread(self, thread_id: Optional[str] = None, *, owner_id: str = "user") -> ThreadState:
        thread_id = thread_id or f"thread-{uuid4().hex[:12]}"
        thread = ThreadState(thread_id=thread_id, owner_id=owner_id)
        self.threads[thread_id] = thread
        return thread

    def get_thread(self, thread_id: str) -> ThreadState:
        if thread_id not in self.threads:
            return self.create_thread(thread_id)
        return self.threads[thread_id]

    def create_topic(self, thread_id: str, *, title: str, summary: str = "") -> TopicState:
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

    def set_pending_clarification(self, thread_id: str, topic_id: str, clarification_id: str) -> None:
        thread = self.get_thread(thread_id)
        thread.pending_clarification_topic_id = topic_id
        thread.pending_clarification_id = clarification_id

    def clear_pending_clarification(self, thread_id: str) -> None:
        thread = self.get_thread(thread_id)
        thread.pending_clarification_topic_id = None
        thread.pending_clarification_id = ""

    def save_clarification_state(self, state: ClarificationState) -> None:
        self.clarification_states[state.run_id] = state

    def get_open_clarification(self, thread_id: str) -> Optional[ClarificationState]:
        topic_ids = set(self.thread_topics.get(thread_id, []))
        for state in reversed(tuple(self.clarification_states.values())):
            if state.status == "waiting" and state.topic_id in topic_ids:
                return state
        return None

    def get_clarification_state(
        self,
        source_run_id: str,
    ) -> Optional[ClarificationState]:
        state = self.clarification_states.get(source_run_id)
        return deepcopy(state) if state is not None else None

    def add_turn(self, thread_id: str, turn: dict) -> None:
        self.get_thread(thread_id).turns.append(turn)

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
            "answer_package": staged_runs.get(run_id, {}).get("answer_package"),
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
            raise EvidenceIntegrityError(
                "terminal_completion_conflict_owner_unproven"
            )
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
            raise EvidenceIntegrityError(
                "terminal_completion_conflict_audit_mismatch"
            )
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
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_value,
        )

        source_run = self.runs.get(source_run_id)
        if not source_run:
            raise EvidenceIntegrityError("clarification_outcome_source_run_missing")
        if str(source_run.get("status") or "") != "waiting_for_clarification":
            raise EvidenceIntegrityError("clarification_outcome_source_run_stale")
        if (
            str(source_run.get("thread_id") or "") != thread_id
            or str(source_run.get("topic_id") or "") != topic_id
        ):
            raise EvidenceIntegrityError("clarification_outcome_owner_mismatch")

        payload = build_clarification_outcome(
            source_run_id=source_run_id,
            thread_id=thread_id,
            topic_id=topic_id,
            choice=choice,
        )
        existing = tuple(
            event
            for event in self._audit_events
            if event.get("event_type") == "clarification_outcome_recorded"
            and event.get("run_id") == source_run_id
        )
        if existing:
            if len(existing) != 1:
                raise EvidenceIntegrityError("clarification_outcome_ambiguous")
            event = existing[0]
            if (
                str(event.get("thread_id") or "") == thread_id
                and str(event.get("topic_id") or "") == topic_id
                and str(event.get("ref") or "") == payload["outcome_ref"]
                and canonical_value(event.get("payload") or {})
                == canonical_value(payload)
            ):
                return str(payload["outcome_ref"])
            raise EvidenceIntegrityError("clarification_outcome_conflict")
        self.add_audit_event(
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

        run = self.runs.get(source_run_id)
        if not run:
            raise EvidenceIntegrityError("clarification_resume_source_run_missing")
        contracts = tuple(
            payload
            for payload in self.analysis_runtime_authority["analysis_contract"].values()
            if str(payload.get("analysis_contract_id") or "")
            == f"analysis:{source_run_id}:1"
        )
        if len(contracts) != 1:
            raise EvidenceIntegrityError("clarification_resume_contract_missing")
        outcome_events = tuple(
            event
            for event in self._audit_events
            if event.get("event_type") == "clarification_outcome_recorded"
            and event.get("run_id") == source_run_id
        )
        if not outcome_events:
            raise EvidenceIntegrityError("clarification_resume_outcome_missing")
        if len(outcome_events) != 1:
            raise EvidenceIntegrityError("clarification_resume_outcome_ambiguous")
        event = outcome_events[0]
        if str(event.get("ref") or "") != outcome_ref:
            raise EvidenceIntegrityError("clarification_resume_outcome_missing")
        run_request = run.get("request") or {}
        material_authority = (
            run_request.get("material_authority")
            if isinstance(run_request, Mapping)
            else None
        )
        return validate_clarification_resume_authority(
            source_run_id=source_run_id,
            thread_id=thread_id,
            topic_id=topic_id,
            choice=choice,
            outcome_ref=outcome_ref,
            analysis_contract=contracts[0],
            stored_contract_signature=str(contracts[0].get("contract_signature") or ""),
            analysis_run_id=source_run_id,
            run_status=str(run.get("status") or ""),
            run_thread_id=str(run.get("thread_id") or ""),
            run_topic_id=str(run.get("topic_id") or ""),
            clarification_outcome=event.get("payload") or {},
            outcome_run_id=str(event.get("run_id") or ""),
            outcome_thread_id=str(event.get("thread_id") or ""),
            outcome_topic_id=str(event.get("topic_id") or ""),
            material_authority=material_authority,
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

        run = self.runs.get(source_run_id)
        if not run:
            raise EvidenceIntegrityError("completed_followup_source_run_missing")
        contract_ref = f"analysis:{source_run_id}:1"
        contract = self.analysis_runtime_authority["analysis_contract"].get(
            contract_ref
        )
        if not isinstance(contract, Mapping):
            raise EvidenceIntegrityError("completed_followup_contract_missing")
        request = run.get("request") or {}
        if not isinstance(request, Mapping):
            raise EvidenceIntegrityError("completed_followup_request_invalid")
        events = tuple(
            event
            for event in self._audit_events
            if event.get("event_type")
            == "completed_material_authority_recorded"
            and event.get("run_id") == source_run_id
        )
        if not events:
            raise EvidenceIntegrityError(
                "completed_followup_authority_record_missing"
            )
        if len(events) != 1:
            raise EvidenceIntegrityError(
                "completed_followup_authority_record_ambiguous"
            )
        event = events[0]
        return validate_completed_followup_authority(
            source_run_id=source_run_id,
            thread_id=thread_id,
            topic_id=topic_id,
            analysis_contract=contract,
            stored_contract_signature=str(contract.get("contract_signature") or ""),
            analysis_run_id=source_run_id,
            run_status=str(run.get("status") or ""),
            run_thread_id=str(run.get("thread_id") or ""),
            run_topic_id=str(run.get("topic_id") or ""),
            request_analysis_contract=request.get("analysis_contract"),
            material_authority=request.get("material_authority"),
            authority_record=event.get("payload") or {},
            authority_event_ref=str(event.get("ref") or ""),
            authority_event_run_id=str(event.get("run_id") or ""),
            authority_event_thread_id=str(event.get("thread_id") or ""),
            authority_event_topic_id=str(event.get("topic_id") or ""),
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

        run = self.runs.get(run_id)
        if not run:
            raise EvidenceIntegrityError("completed_followup_source_run_missing")
        if (
            str(run.get("thread_id") or "") != thread_id
            or str(run.get("topic_id") or "") != topic_id
        ):
            raise EvidenceIntegrityError("completed_followup_owner_mismatch")
        run_status = str(run.get("status") or "")
        contract = self.analysis_runtime_authority["analysis_contract"].get(
            f"analysis:{run_id}:1"
        )
        if not isinstance(contract, Mapping):
            raise EvidenceIntegrityError("completed_followup_contract_missing")
        finalized_request = canonical_value(
            {
                **dict(request),
                "analysis_contract": contract,
                "material_authority": material_authority,
            }
        )
        record = build_completed_material_authority_record(
            source_run_id=run_id,
            thread_id=thread_id,
            topic_id=topic_id,
            analysis_contract=contract,
            material_authority=material_authority,
        )
        validate_completed_followup_authority(
            source_run_id=run_id,
            thread_id=thread_id,
            topic_id=topic_id,
            analysis_contract=contract,
            stored_contract_signature=str(contract.get("contract_signature") or ""),
            analysis_run_id=run_id,
            run_status="completed",
            run_thread_id=thread_id,
            run_topic_id=topic_id,
            request_analysis_contract=finalized_request["analysis_contract"],
            material_authority=finalized_request["material_authority"],
            authority_record=record,
            authority_event_ref=f"completed-material-authority:{run_id}",
            authority_event_run_id=run_id,
            authority_event_thread_id=thread_id,
            authority_event_topic_id=topic_id,
        )
        existing = tuple(
            event
            for event in self._audit_events
            if event.get("event_type")
            == "completed_material_authority_recorded"
            and event.get("run_id") == run_id
        )
        if run_status == "completed":
            if not existing:
                raise EvidenceIntegrityError(
                    "completed_followup_source_run_not_finalizable"
                )
            if (
                len(existing) != 1
                or canonical_value(existing[0].get("payload") or {}) != record
                or str(existing[0].get("ref") or "")
                != f"completed-material-authority:{run_id}"
                or str(existing[0].get("run_id") or "") != run_id
                or str(existing[0].get("thread_id") or "") != thread_id
                or str(existing[0].get("topic_id") or "") != topic_id
                or canonical_value(run.get("request") or {}) != finalized_request
                or str(run.get("status") or "") != "completed"
            ):
                raise EvidenceIntegrityError(
                    "completed_followup_authority_record_conflict"
                )
            return deepcopy(finalized_request)
        if run_status != "running_workflow":
            raise EvidenceIntegrityError(
                "completed_followup_source_run_not_finalizable"
            )
        if existing:
            raise EvidenceIntegrityError(
                "completed_followup_authority_record_conflict"
            )
        staged_runs = deepcopy(self.runs)
        staged_events = deepcopy(self._audit_events)
        staged_runs[run_id] = {
            **run,
            "status": "completed",
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
                "payload": {"status": "completed"},
            },
        )
        self._append_staged_audit_event(
            staged_events,
            {
                "event_type": "completed_material_authority_recorded",
                "thread_id": thread_id,
                "topic_id": topic_id,
                "run_id": run_id,
                "ref": f"completed-material-authority:{run_id}",
                "payload": record,
            },
        )
        self.runs = staged_runs
        self._audit_events = staged_events
        return deepcopy(finalized_request)

    def record_context_manifest(self, manifest: dict) -> None:
        self.save_context_manifest(manifest)

    def save_context_manifest(self, manifest: ContextManifest | dict) -> None:
        from bi_agent.runtime.claim_provenance import (
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
        authority = self.analysis_runtime_authority.get(
            "context_manifest", {}
        ).get(manifest_id)
        for stored in (existing, authority):
            if stored is not None and canonical_value(stored) != payload:
                raise EvidenceIntegrityError(
                    "context_manifest_publication_conflict"
                )
        if existing is not None or authority is not None:
            self.context_manifests.setdefault(manifest_id, deepcopy(payload))
            return
        self.context_manifests[manifest_id] = deepcopy(payload)
        self.add_audit_event(
            "context_manifest_recorded",
            thread_id=payload.get("thread_id", ""),
            ref=manifest_id,
        )

    def save_reuse_decisions(
        self,
        thread_id: str,
        turn_id: str,
        decisions: tuple[ReuseDecision, ...] | list[ReuseDecision],
    ) -> None:
        self.reuse_decisions[(thread_id, turn_id)] = [
            decision.to_dict() if hasattr(decision, "to_dict") else dict(decision)
            for decision in decisions
        ]
        self.add_audit_event(
            "reuse_decisions_recorded",
            thread_id=thread_id,
            ref=turn_id,
            payload={"decisions": self.reuse_decisions[(thread_id, turn_id)]},
        )

    def list_context_manifests(self, thread_id: str) -> tuple[ContextManifest, ...]:
        return tuple(
            _context_manifest_from_payload(payload)
            for payload in self.context_manifests.values()
            if payload.get("thread_id") == thread_id
        )

    def record_answer_package(self, run_id: str, package: dict) -> None:
        self.answer_packages[run_id] = package
        if run_id in self.runs:
            self.runs[run_id]["answer_package"] = package
            topic_id = self.runs[run_id].get("topic_id")
            artifact_id = package.get("artifact_id") or package.get("artifact_path") or f"answer-package:{run_id}"
            if topic_id:
                self.add_artifact(
                    artifact_id=artifact_id,
                    topic_id=topic_id,
                    follow_up_context=_package_follow_up_context(package),
                    snapshot_id=package.get("snapshot_id") or package.get("snapshot") or "unknown",
                    permission_scope=package.get("permission_scope") or package.get("visibility") or "analyst",
                )
        self.add_audit_event("answer_package_recorded", run_id=run_id, ref=run_id)

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
        answer_package_artifacts: Sequence[Mapping[str, Any]] | None = None,
        verified_claims: Sequence[Mapping[str, Any]],
        claim_links: Sequence[Mapping[str, Any]],
        repair_attempts: Sequence[Mapping[str, Any]],
    ) -> str:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
            canonical_value,
        )
        from bi_agent.runtime.runtime_persistence import (
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
            answer_package_artifacts=answer_package_artifacts,
            verified_claims=verified_claims,
            claim_links=claim_links,
            repair_attempts=repair_attempts,
            result_candidate_resolver=self.resolve_result_candidate_authority,
        )
        frozen = canonical_value(bundle)
        digest = canonical_digest(frozen)
        existing_publication = self.analysis_runtime_records.get(run_id)
        if existing_publication is not None:
            if existing_publication["digest"] == digest:
                return "replayed"
            raise EvidenceIntegrityError("analysis_runtime_publication_conflict")
        entries = (
            (
                "analysis_contract",
                ((bundle["analysis_contract"]["analysis_contract_id"], bundle["analysis_contract"]),),
            ),
            (
                "query_contract",
                tuple((item.query_contract_id, canonical_value(item)) for item in bundle["query_contracts"]),
            ),
            (
                "query_execution_record",
                tuple((item.record_ref, canonical_value(item)) for item in bundle["query_execution_records"]),
            ),
            (
                "rows_record",
                tuple((item.record_ref, canonical_value(item)) for item in bundle["rows_records"]),
            ),
            (
                "snapshot_record",
                tuple((item.record_ref, canonical_value(item)) for item in bundle["snapshot_records"]),
            ),
            (
                "completeness_record",
                tuple((item.record_ref, canonical_value(item)) for item in bundle["completeness_records"]),
            ),
            (
                "capability_binding_record",
                tuple((item.record_ref, canonical_value(item)) for item in bundle["capability_binding_records"]),
            ),
            (
                "evidence_manifest",
                tuple((item["evidence_ref"], item) for item in bundle["evidence_manifests"]),
            ),
            (
                "context_manifest",
                tuple((item["manifest_id"], item) for item in bundle["context_manifests"]),
            ),
            (
                "claim_provenance_record",
                tuple(
                    (item["record_ref"], item)
                    for item in bundle["trusted_provenance_records"]
                ),
            ),
            (
                "answer_package_artifact",
                tuple(
                    (item["artifact_ref"], item)
                    for item in bundle["answer_package_artifacts"]
                ),
            ),
            (
                "verified_claim",
                tuple((item["claim_ref"], item) for item in bundle["verified_claims"]),
            ),
            (
                "claim_evidence_link",
                tuple(
                    (f"{item['claim_ref']}\x1f{item['evidence_ref']}", item)
                    for item in bundle["claim_links"]
                ),
            ),
            (
                "repair_attempt",
                tuple((item["attempt_ref"], item) for item in bundle["repair_attempts"]),
            ),
        )
        staged_authority = deepcopy(self.analysis_runtime_authority)
        staged_context_manifests = deepcopy(self.context_manifests)
        for kind, records in entries:
            existing_records = staged_authority[kind]
            for ref, payload in records:
                existing = existing_records.get(str(ref))
                if existing is not None and existing != canonical_value(payload):
                    raise EvidenceIntegrityError(f"authority_ref_collision:{kind}")
                if kind == "context_manifest":
                    published = staged_context_manifests.get(str(ref))
                    if (
                        published is not None
                        and canonical_value(published) != canonical_value(payload)
                    ):
                        raise EvidenceIntegrityError(
                            "context_manifest_publication_conflict"
                        )
        for kind, records in entries:
            target = staged_authority[kind]
            for ref, payload in records:
                target[str(ref)] = deepcopy(canonical_value(payload))
                if kind == "context_manifest":
                    staged_context_manifests[str(ref)] = deepcopy(
                        canonical_value(payload)
                    )
        staged_publications = deepcopy(self.analysis_runtime_records)
        staged_publications[run_id] = {
            "digest": digest,
            "payload": deepcopy(frozen),
        }
        staged_audits = deepcopy(self._audit_events)
        staged_audits.append(
            {
                "event_type": "analysis_runtime_records_persisted",
                "thread_id": "",
                "topic_id": "",
                "run_id": run_id,
                "ref": str(bundle["analysis_contract"]["analysis_contract_id"]),
                "payload": deepcopy(
                    {
                        "bundle_digest": digest,
                        "query_count": len(bundle["query_execution_records"]),
                        "capability_binding_count": len(
                            bundle["capability_binding_records"]
                        ),
                        "claim_link_count": len(bundle["claim_links"]),
                    }
                ),
            }
        )
        self.analysis_runtime_authority = staged_authority
        self.context_manifests = staged_context_manifests
        self.analysis_runtime_records = staged_publications
        self._audit_events = staged_audits
        return "published"

    def save_analysis_assets(
        self,
        thread_id: str,
        topic_id: str,
        assets: Sequence[Mapping[str, Any]],
    ) -> None:
        key = (thread_id, topic_id)
        self.analysis_assets[key] = list(
            merge_analysis_assets(self.analysis_assets.get(key, ()), assets)
        )
        self.add_audit_event(
            "analysis_assets_recorded",
            thread_id=thread_id,
            topic_id=topic_id,
            ref=topic_id,
            payload={"count": len(assets)},
        )

    def list_analysis_assets(self, thread_id: str, topic_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(dict(asset) for asset in self.analysis_assets.get((thread_id, topic_id), ()))

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

    def list_dataset_snapshots(self, dataset_id: str = "") -> tuple[dict[str, Any], ...]:
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
            if (
                existing.get("status") != payload.get("status")
                or immutable_dataset_snapshot_projection(existing)
                != immutable_dataset_snapshot_projection(payload)
            ):
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
                    str(release.get("member_projections", [{}])[0].get("dataset_id") or "")
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

    def record_run_nodes(self, run_id: str, checkpoint_events: tuple[dict, ...]) -> None:
        self.runs.setdefault(run_id, {})["checkpoint_events"] = list(checkpoint_events)
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
        record = ResultRefRecord(
            topic_id=topic_id,
            result_ref=result_ref,
            snapshot_id=snapshot_id,
            contract_version=contract_version,
            permission_scope=permission_scope,
            semantic_scope=semantic_scope,
            payload=deepcopy(candidate_payload),
        )
        existing = next(
            (
                item
                for records in self.result_refs.values()
                for item in records
                if item.result_ref == result_ref
            ),
            None,
        )
        if existing is not None:
            if existing.to_dict() != record.to_dict():
                raise EvidenceIntegrityError("result_ref_payload_conflict")
            return
        self.result_refs[topic_id].insert(0, record)

    def results_for_topic(self, topic_id: Optional[str]) -> tuple[ResultRefRecord, ...]:
        if not topic_id:
            return ()
        return tuple(deepcopy(self.result_refs.get(topic_id, ())))

    def resolve_result_candidate_authority(
        self,
        *,
        result_ref: str,
        topic_id: str,
    ) -> dict[str, Any]:
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            canonical_digest,
            canonical_value,
        )
        from bi_agent.runtime.runtime_persistence import (
            result_candidate_publication_authority_projection,
            validate_result_candidate_publication_authority,
        )

        matches = tuple(
            item
            for item in self.result_refs.get(topic_id, ())
            if item.result_ref == result_ref and item.payload
        )
        if len(matches) != 1:
            raise EvidenceIntegrityError("result_candidate_authority_missing")
        record = matches[0]
        payload = validate_result_reuse_candidate(record.payload)
        source_run_id = payload["source_run_id"]
        run = self.runs.get(source_run_id)
        if not run or str(run.get("topic_id") or "") != topic_id:
            raise EvidenceIntegrityError("result_candidate_source_run_missing")
        publication = self.analysis_runtime_records.get(source_run_id)
        publication_payload = (
            publication.get("payload")
            if isinstance(publication, Mapping)
            else None
        )
        publication_digest = (
            str(publication.get("digest") or "")
            if isinstance(publication, Mapping)
            else ""
        )
        if (
            not isinstance(publication_payload, Mapping)
            or publication_digest != canonical_digest(publication_payload)
        ):
            raise EvidenceIntegrityError(
                "result_candidate_source_publication_mismatch:digest"
            )
        validate_result_candidate_publication_authority(
            payload,
            publication_payload,
        )
        contract = (
            publication_payload.get("analysis_contract")
            if isinstance(publication_payload, Mapping)
            else None
        )
        if (
            not isinstance(contract, Mapping)
            or str(contract.get("analysis_contract_id") or "")
            != payload["analysis_contract_ref"]
        ):
            raise EvidenceIntegrityError("result_candidate_analysis_contract_missing")
        authority_contract = self.analysis_runtime_authority[
            "analysis_contract"
        ].get(payload["analysis_contract_ref"])
        if (
            not isinstance(authority_contract, Mapping)
            or canonical_value(authority_contract) != canonical_value(contract)
        ):
            raise EvidenceIntegrityError("result_candidate_analysis_contract_mismatch")
        stored_signature = str(contract.get("contract_signature") or "")
        try:
            completed_authority = self.resolve_completed_material_authority(
                source_run_id=source_run_id,
                thread_id=str(run.get("thread_id") or ""),
                topic_id=topic_id,
            )
        except EvidenceIntegrityError as exc:
            raise EvidenceIntegrityError(
                "result_candidate_completed_authority_invalid"
            ) from exc
        completed_contract = {
            **dict(completed_authority.get("analysis_contract") or {}),
            "contract_signature": str(
                completed_authority.get("analysis_contract_signature") or ""
            ),
        }
        context_manifest = run.get("request", {}).get("context_manifest", {})
        contract_versions = (
            context_manifest.get("contract_versions", {})
            if isinstance(context_manifest, Mapping)
            else {}
        )
        if (
            stored_signature != payload["analysis_contract_signature"]
            or record.result_ref != payload["result_ref"]
            or record.snapshot_id != payload["runtime_snapshot_id"]
            or record.contract_version != payload["runtime_contract_version"]
            or record.permission_scope != payload["permission_scope"]
            or record.semantic_scope != payload["semantic_scope_signature"]
            or not isinstance(context_manifest, Mapping)
            or str(context_manifest.get("snapshot_version") or "")
            != payload["runtime_snapshot_id"]
            or not isinstance(contract_versions, Mapping)
            or str(contract_versions.get("runtime") or "")
            != payload["runtime_contract_version"]
            or canonical_value(completed_contract) != canonical_value(contract)
        ):
            raise EvidenceIntegrityError("result_candidate_analysis_contract_mismatch")
        return canonical_value(
            {
                "result_ref_record": record.to_dict(),
                "source_run_id": source_run_id,
                "run_thread_id": str(run.get("thread_id") or ""),
                "run_topic_id": str(run.get("topic_id") or ""),
                "run_status": str(run.get("status") or ""),
                "source_run_request": dict(run.get("request") or {}),
                "analysis_contract": dict(contract),
                "stored_analysis_contract_signature": stored_signature,
                "cache_authority": (
                    result_candidate_publication_authority_projection(
                        payload,
                        publication_payload,
                    )
                ),
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
    ) -> None:
        self.artifacts[artifact_id] = ArtifactRef(
            artifact_id=artifact_id,
            topic_id=topic_id,
            follow_up_context=follow_up_context,
            snapshot_id=snapshot_id,
            permission_scope=permission_scope,
        )

    def latest_artifact_for_topic(self, topic_id: Optional[str]) -> Optional[ArtifactRef]:
        if not topic_id:
            return None
        for artifact in reversed(tuple(self.artifacts.values())):
            if artifact.topic_id == topic_id:
                return artifact
        return None

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
        self.memory_items[owner_scope].append(item)
        return item

    def long_term_memory(self, owner_scope: str) -> tuple[MemoryItem, ...]:
        return tuple(item for item in self.memory_items.get(owner_scope, ()) if item.status == "accepted")

    def add_memory_proposal(self, proposal: MemoryProposal) -> None:
        self.memory_proposals[proposal.proposal_id] = proposal

    def accept_memory_proposal(self, proposal_id: str) -> Optional[MemoryItem]:
        proposal = self.memory_proposals.get(proposal_id)
        if not proposal:
            return None
        return self.add_memory_item(
            owner_scope=proposal.owner_scope,
            text=proposal.text,
            source_ref=proposal.source_ref,
            visibility=proposal.visibility,
            status="accepted",
        )


def _package_follow_up_context(package: dict) -> str:
    for key in ("follow_up_context", "business_summary", "final_answer", "answer", "summary"):
        value = package.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "已验证 Answer Package，可作为继续调查上下文。"


def _context_manifest_from_payload(payload: dict) -> ContextManifest:
    return ContextManifest(
        manifest_id=payload["manifest_id"],
        thread_id=payload["thread_id"],
        turn_id=payload.get("turn_id", ""),
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
        can_support_claims=bool(payload.get("can_support_claims")),
    )
