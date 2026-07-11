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
            verified_claims=verified_claims,
            claim_links=claim_links,
            repair_attempts=repair_attempts,
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
        for kind, records in entries:
            existing_records = staged_authority[kind]
            for ref, payload in records:
                existing = existing_records.get(str(ref))
                if existing is not None and existing != canonical_value(payload):
                    raise EvidenceIntegrityError(f"authority_ref_collision:{kind}")
        for kind, records in entries:
            target = staged_authority[kind]
            for ref, payload in records:
                target[str(ref)] = deepcopy(canonical_value(payload))
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
        self._audit_events.append(
            {
                "event_type": event_type,
                "thread_id": thread_id,
                "topic_id": topic_id,
                "run_id": run_id,
                "ref": ref,
                "payload": deepcopy(payload) if payload is not None else {},
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
