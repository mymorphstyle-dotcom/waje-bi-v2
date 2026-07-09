from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
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
        self.clarification_states: dict[str, ClarificationState] = {}
        self.audit_events: list[dict] = []

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
        self.runs[run_id] = {
            "run_id": run_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "topic_id": topic_id,
            "status": status,
            "request": request or {},
            "answer_package": self.runs.get(run_id, {}).get("answer_package"),
        }
        self.add_audit_event("run_status_changed", thread_id=thread_id, topic_id=topic_id, run_id=run_id)

    def record_context_manifest(self, manifest: dict) -> None:
        self.save_context_manifest(manifest)

    def save_context_manifest(self, manifest: ContextManifest | dict) -> None:
        payload = manifest.to_dict() if hasattr(manifest, "to_dict") else dict(manifest)
        self.context_manifests[payload["manifest_id"]] = payload
        self.add_audit_event(
            "context_manifest_recorded",
            thread_id=payload.get("thread_id", ""),
            ref=payload["manifest_id"],
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

    def save_analysis_assets(
        self,
        thread_id: str,
        topic_id: str,
        assets: Sequence[Mapping[str, Any]],
    ) -> None:
        self.analysis_assets[(thread_id, topic_id)].extend(dict(asset) for asset in assets)
        self.add_audit_event(
            "analysis_assets_recorded",
            thread_id=thread_id,
            topic_id=topic_id,
            ref=topic_id,
            payload={"count": len(assets)},
        )

    def list_analysis_assets(self, thread_id: str, topic_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(dict(asset) for asset in self.analysis_assets.get((thread_id, topic_id), ()))

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
        self.audit_events.append(
            {
                "event_type": event_type,
                "thread_id": thread_id,
                "topic_id": topic_id,
                "run_id": run_id,
                "ref": ref,
                "payload": payload or {},
            }
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
    ) -> None:
        self.result_refs[topic_id].append(
            ResultRefRecord(
                topic_id=topic_id,
                result_ref=result_ref,
                snapshot_id=snapshot_id,
                contract_version=contract_version,
                permission_scope=permission_scope,
                semantic_scope=semantic_scope,
            )
        )

    def results_for_topic(self, topic_id: Optional[str]) -> tuple[ResultRefRecord, ...]:
        if not topic_id:
            return ()
        return tuple(self.result_refs.get(topic_id, ()))

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
        created_at=payload.get("created_at"),
        can_support_claims=bool(payload.get("can_support_claims")),
    )
