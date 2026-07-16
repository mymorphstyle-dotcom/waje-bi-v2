from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
import re
from types import MappingProxyType
from typing import Any, Mapping, Optional
from uuid import uuid4

from bi_agent.conversation.models import (
    CLARIFICATION_ESCAPE_OPTION,
    ClarificationOption,
    ClarificationQuestion,
    ClarificationRequest,
    ClarificationState,
    ContextItem,
    ContextManifest,
    ConversationRunRequest,
    ConversationTurnResult,
    MemoryProposal,
    ReuseDecision,
    TopicState,
    TurnIntent,
    validate_result_reuse_candidate,
)
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.conversation.clarification_authority import (
    build_prior_topic_material_context,
)
from bi_agent.conversation.clarification_options import (
    clarification_labels_match,
)
from bi_agent.runtime.analysis_assets import merge_analysis_assets
from bi_agent.runtime.compiler import suggest_revenue_diagnostic_nodes
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)
from bi_agent.runtime.permission_roles import can_read_scope as _can_read_permission_scope
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


ALLOWED_INTENTS = frozenset(
    {
        "new_topic",
        "follow_up",
        "mixed_question",
        "correction",
        "clarification_answer",
        "challenge",
        "artifact_continue",
        "capability_question",
        "off_topic",
        "unsupported_request",
        "memory_update",
    }
)
ALLOWED_TOPIC_RELATIONS = frozenset(
    {
        "new_topic",
        "inherit_current",
        "split_topics",
        "split_subintents",
        "select_referenced_topic",
        "ask_topic_choice",
        "queued_new_topic",
        "rejected",
    }
)
LOCAL_GUARDED_INTENTS = frozenset({"off_topic", "unsupported_request"})


class ConversationOrchestrationError(RuntimeError):
    pass


class ConversationRuntime:
    def __init__(
        self,
        store: Optional[InMemoryConversationStore] = None,
        *,
        llm_client: Any = None,
    ) -> None:
        self.store = store or InMemoryConversationStore()
        self.llm_client = llm_client

    def handle_message(
        self,
        thread_id: str,
        user_message: str,
        *,
        role: str = "analyst",
        active_run_status: str = "idle",
        current_snapshot: str = "2026H1",
        contract_version: str = "contracts-v1",
        owner_scope: str = "org-default",
        run_id: str | None = None,
        prior_analysis_assets: tuple[Mapping[str, Any], ...] = (),
        analysis_context: Mapping[str, Any] | None = None,
        clarification_resume_claim: Mapping[str, Any] | None = None,
    ) -> ConversationTurnResult:
        thread = self.store.get_thread(thread_id)
        explicit_source_run_id = str(
            (clarification_resume_claim or {}).get("source_run_id") or ""
        )
        if explicit_source_run_id:
            if (
                str(
                    (clarification_resume_claim or {}).get("resumed_run_id")
                    or ""
                )
                != str(run_id or "")
                or str(
                    (clarification_resume_claim or {}).get("thread_id") or ""
                )
                != thread_id
            ):
                raise ConversationOrchestrationError(
                    "clarification_resume_claim_owner_mismatch"
                )
            get_clarification_state = getattr(
                self.store,
                "get_clarification_state",
                None,
            )
            if not callable(get_clarification_state):
                raise ConversationOrchestrationError(
                    "clarification_source_state_resolver_missing"
                )
            open_clarification = get_clarification_state(
                explicit_source_run_id
            )
            if open_clarification is None:
                raise ConversationOrchestrationError(
                    "clarification_source_state_missing"
                )
        else:
            open_clarification = self.store.get_open_clarification(thread_id)
        text = user_message.strip()
        matches_open_clarification = (
            True
            if explicit_source_run_id and open_clarification
            else _looks_like_clarification_answer(text, open_clarification)
            if open_clarification
            else False
        )
        prior_request: dict[str, Any] = {}
        clarification_source: dict[str, Any] = {}
        selected_query_gap_action: dict[str, Any] = {}
        selected_material_action: dict[str, Any] = {}
        accepted_degradation_choice: dict[str, Any] = {}
        if open_clarification and matches_open_clarification:
            get_run_request = getattr(self.store, "get_run_request", None)
            if not callable(get_run_request):
                raise ConversationOrchestrationError(
                    "clarification_source_envelope_invalid"
                )
            prior_request = dict(get_run_request(open_clarification.run_id) or {})
            clarification_source = _clarification_source_from_request(
                prior_request,
                source_run_id=open_clarification.run_id,
                source_thread_id=thread_id,
                source_topic_id=open_clarification.topic_id,
                source_owner_id=thread.owner_id,
            )
            prior_clarification = dict(
                clarification_source.get("clarification") or {}
            )
            selected_query_gap_action = _selected_query_gap_action(
                prior_clarification,
                user_message,
                selected_option_id=str(
                    (clarification_resume_claim or {}).get(
                        "selected_option_id"
                    )
                    or ""
                ),
                clarification_state=open_clarification,
            )
            if str(
                selected_query_gap_action.get("action_kind") or ""
            ) == "bind_material_choice":
                selected_material_action = dict(selected_query_gap_action)
            if selected_query_gap_action and str(
                selected_query_gap_action.get("action_kind") or ""
            ) not in {
                "bind_material_choice",
                "wait_for_source",
                "user_redirect",
            }:
                accepted_degradation_choice = {
                    **selected_query_gap_action,
                    "source_run_id": open_clarification.run_id,
                }
        if open_clarification and matches_open_clarification:
            self.store.set_pending_clarification(
                thread_id,
                open_clarification.topic_id,
                (
                    open_clarification.run_id
                    if explicit_source_run_id
                    else thread.pending_clarification_id
                    or open_clarification.run_id
                ),
            )
            thread = self.store.get_thread(thread_id)
        turn_id = f"turn-{uuid4().hex[:12]}"
        allow_clarification_answer = matches_open_clarification
        local_intent = _classify_intent(user_message, allow_clarification_answer)
        local_topic_relation = _topic_relation(local_intent, user_message, active_run_status)
        if (
            not thread.current_topic_id
            and local_topic_relation == "inherit_current"
            and _should_run(local_intent, local_topic_relation)
        ):
            local_intent = "new_topic"
            local_topic_relation = "new_topic"
        orchestration = self._orchestrate_turn(
            thread_id,
            thread,
            user_message,
            active_run_status,
            local_intent,
            local_topic_relation,
            allow_clarification_answer,
        )
        intent_name = orchestration["intent"]
        topic_relation = orchestration["topic_relation"]
        pending_clarification_id = (
            open_clarification.run_id
            if explicit_source_run_id and open_clarification
            else thread.pending_clarification_id
        )
        topic = self._resolve_topic(thread_id, topic_relation, user_message, intent_name)
        turn_intent = TurnIntent(
            intent=intent_name,
            confidence=orchestration["confidence"],
            topic_relation=topic_relation,
            decision_source=orchestration["decision_source"],
            business_summary=orchestration["business_summary"],
        )
        reuse_decisions = self._reuse_decisions(
            topic,
            intent_name,
            topic_relation,
            user_message,
            role,
            current_snapshot,
            contract_version,
        )
        reuse_candidates = self._reuse_candidate_payloads(topic, reuse_decisions)
        material_context_candidates = self._material_context_candidate_payloads(
            topic,
            reuse_decisions,
        )
        prior_topic_material_context: dict[str, Any] = {}
        if (
            topic_relation == "inherit_current"
            and topic is not None
            and material_context_candidates
        ):
            prior_topic_material_context = (
                self._validated_prior_topic_material_context(
                    thread_id=thread_id,
                    topic=topic,
                    role=role,
                    candidates=material_context_candidates,
                )
            )
        topic_assets = self._topic_analysis_assets(thread_id, topic)
        combined_prior_assets = merge_analysis_assets(topic_assets, prior_analysis_assets)
        manifest = self._context_manifest(
            thread_id,
            turn_id,
            topic,
            user_message,
            role,
            current_snapshot,
            contract_version,
            reuse_decisions,
            owner_scope,
            combined_prior_assets,
            pending_clarification_id if intent_name == "clarification_answer" else "",
            prior_topic_material_context=prior_topic_material_context,
            intent=intent_name,
            accepted_assumptions=(
                (accepted_degradation_choice,)
                if isinstance(accepted_degradation_choice, Mapping)
                and accepted_degradation_choice
                else ()
            ),
        )
        memory_proposals = self._memory_proposals(
            thread_id,
            turn_id,
            user_message,
            intent_name,
            owner_scope,
            role,
        )
        for proposal in memory_proposals:
            self.store.add_memory_proposal(proposal)
        needs_clarification = intent_name != "clarification_answer" and (
            topic_relation == "ask_topic_choice" or _needs_clarification(user_message)
        )
        clarification = (
            _build_clarification(turn_id, user_message, topic_relation)
            if needs_clarification
            else None
        )
        clarification_topic = topic or self.store.current_topic(thread_id)
        if clarification and clarification_topic:
            state_run_id = run_id or clarification.clarification_id
            self.store.set_pending_clarification(
                thread_id,
                clarification_topic.topic_id,
                clarification.clarification_id,
            )
            self.store.save_clarification_state(
                ClarificationState(
                    run_id=state_run_id,
                    topic_id=clarification_topic.topic_id,
                    question=clarification.questions[0].question,
                    options=list(clarification.questions[0].options),
                )
            )
        run_request = None
        if not needs_clarification and _should_run(intent_name, topic_relation):
            clarification_resume_context = {}
            if intent_name == "clarification_answer" and open_clarification:
                if prior_request:
                    prior_clarification = dict(
                        clarification_source.get("clarification") or {}
                    )
                    source_material = dict(
                        clarification_source.get("source_material") or {}
                    )
                    clarification_resume_context = {
                        "resume_run_id": str(
                            clarification_source.get("source_run_id") or ""
                        ),
                        "source_thread_id": str(
                            clarification_source.get("source_thread_id") or ""
                        ),
                        "source_topic_id": str(
                            clarification_source.get("source_topic_id") or ""
                        ),
                        "question": clarification_source["question"],
                        "accepted_graph": tuple(
                            source_material.get("accepted_graph") or ()
                        ),
                        "analysis_contract": dict(
                            source_material.get("analysis_contract") or {}
                        ),
                        "analysis_route": dict(
                            source_material.get("analysis_route") or {}
                        ),
                        "analysis_context": dict(
                            clarification_source.get("analysis_context") or {}
                        ),
                        "original_intent": dict(
                            source_material.get("original_intent") or {}
                        ),
                        "material_slots": dict(
                            source_material.get("material_slots") or {}
                        ),
                        "clarification": prior_clarification,
                        "selected_query_gap_action": selected_query_gap_action,
                        "selected_material_action": selected_material_action,
                        "accepted_degradation_choice": accepted_degradation_choice,
                    }
            request_analysis_context = dict(analysis_context or {})
            request_user_message = user_message
            if clarification_resume_context:
                request_analysis_context = dict(
                    clarification_resume_context.get("analysis_context") or {}
                )
                request_user_message = str(
                    clarification_resume_context["question"]
                )
            run_request = ConversationRunRequest(
                thread_id=thread_id,
                turn_id=turn_id,
                topic_id=topic.topic_id if topic else None,
                user_message=request_user_message,
                context_manifest=manifest.to_dict(),
                permission_context={"role": role},
                runtime_budget=_runtime_budget(user_message),
                analysis_context=request_analysis_context,
                clarification_resume_context=clarification_resume_context,
                prior_analysis_assets=combined_prior_assets,
                reuse_candidates=reuse_candidates,
                prior_topic_material_context=prior_topic_material_context,
                requested_nodes=_requested_nodes(user_message, intent_name),
            )
        audit_events = (
            {
                "event": "turn_intent_bound",
                "turn_id": turn_id,
                "intent": intent_name,
                "topic_relation": topic_relation,
                "source": turn_intent.decision_source,
                "local_intent": local_intent,
                "local_topic_relation": local_topic_relation,
            },
            {
                "event": "context_manifest_created",
                "turn_id": turn_id,
                "manifest_id": manifest.manifest_id,
                "can_support_claims": manifest.can_support_claims,
            },
        )
        if orchestration.get("llm_audit"):
            audit_events = audit_events + (
                {
                    "event": "conversation_orchestrator_llm_evaluated",
                    "turn_id": turn_id,
                    "source": turn_intent.decision_source,
                    "audit": orchestration["llm_audit"],
                },
            )
        if clarification:
            audit_events = audit_events + (
                {
                    "event": "clarification_requested",
                    "turn_id": turn_id,
                    "clarification_id": clarification.clarification_id,
                    "reason": clarification.reason,
                },
            )
            self.store.add_audit_event(
                "clarification_requested",
                thread_id=thread_id,
                topic_id=topic.topic_id if topic else "",
                ref=clarification.clarification_id,
                payload=clarification.to_dict(),
            )
        result = ConversationTurnResult(
            thread_id=thread_id,
            turn_id=turn_id,
            topic_id=topic.topic_id if topic else None,
            turn_intent=turn_intent,
            topic_relation=topic_relation,
            context_manifest=manifest,
            reuse_decisions=reuse_decisions,
            memory_proposals=memory_proposals,
            audit_events=audit_events,
            run_request=run_request,
            needs_clarification=needs_clarification,
            clarification=clarification,
            response_boundary=_response_boundary(intent_name),
        )
        self.store.add_turn(thread_id, result.to_dict())
        self.store.save_context_manifest(manifest)
        self.store.save_reuse_decisions(thread_id, turn_id, reuse_decisions)
        if intent_name == "clarification_answer":
            if open_clarification:
                self.store.save_clarification_state(
                    replace(open_clarification, status="answered", answer=user_message)
                )
            self.store.clear_pending_clarification(thread_id)
        return result

    def _orchestrate_turn(
        self,
        thread_id: str,
        thread: Any,
        message: str,
        active_run_status: str,
        local_intent: str,
        local_topic_relation: str,
        allow_clarification_answer: bool,
    ) -> dict[str, Any]:
        local = _local_orchestration(local_intent, local_topic_relation, message)
        if not self.llm_client:
            return local
        if not _should_use_llm_orchestrator(
            local_intent,
            local_topic_relation,
            message,
            allow_clarification_answer,
        ):
            return local

        from bi_agent.runtime.llm_prompts import build_prompt

        spec = build_prompt(
            "conversation_orchestrator",
            {
                "user_message": message,
                "thread_state": {
                    "thread_id": thread_id,
                    "current_topic_id": thread.current_topic_id,
                    "pending_clarification_id": thread.pending_clarification_id,
                    "pending_clarification_topic_id": thread.pending_clarification_topic_id,
                    "active_run_status": active_run_status,
                },
                "candidate_topics": [
                    topic.to_dict() for topic in self.store.topics_for_thread(thread_id)[-5:]
                ],
                "recent_turns": list(getattr(thread, "turns", [])[-5:]),
                "local_precheck": {
                    "intent": local_intent,
                    "topic_relation": local_topic_relation,
                },
                "allowed_intents": sorted(ALLOWED_INTENTS),
                "allowed_topic_relations": sorted(ALLOWED_TOPIC_RELATIONS),
            },
        )
        try:
            result = self.llm_client.invoke_json(
                task=spec.task,
                prompt_version=spec.prompt_version,
                messages=spec.messages,
                required_keys=spec.required_keys,
            )
        except Exception as exc:
            raise ConversationOrchestrationError(
                "conversation_orchestrator_provider_failed"
            ) from exc

        validated = _validated_orchestration(
            result.output,
            local,
            allow_clarification_answer=allow_clarification_answer,
            active_run_status=active_run_status,
            topic_count=len(self.store.topics_for_thread(thread_id)),
        )
        validated["llm_audit"] = result.audit
        return validated

    def _resolve_topic(
        self,
        thread_id: str,
        relation: str,
        message: str,
        intent: str,
    ) -> Optional[TopicState]:
        if intent == "clarification_answer":
            thread = self.store.get_thread(thread_id)
            topic = self.store.topic(thread.pending_clarification_topic_id)
            if topic:
                self.store.set_current_topic(thread_id, topic.topic_id)
                return topic
        if relation in {"rejected", "ask_topic_choice"}:
            return self.store.current_topic(thread_id) if "老板" in message else None
        if relation == "select_referenced_topic":
            topics = self.store.topics_for_thread(thread_id)
            if len(topics) >= 2:
                self.store.set_current_topic(thread_id, topics[1].topic_id)
                return topics[1]
        if relation in {"new_topic", "queued_new_topic"}:
            topic = self.store.create_topic(thread_id, title=_topic_title(message), summary=message)
            self.store.set_current_topic(thread_id, topic.topic_id)
            return topic
        return self.store.current_topic(thread_id)

    def _reuse_decisions(
        self,
        topic: Optional[TopicState],
        intent: str,
        relation: str,
        message: str,
        role: str,
        current_snapshot: str,
        contract_version: str,
    ) -> tuple[ReuseDecision, ...]:
        if relation == "ask_topic_choice":
            return (ReuseDecision("none", "", "topic_reference_ambiguous"),)
        if intent in {"off_topic", "capability_question", "memory_update"}:
            return (ReuseDecision("none", "", "no_bi_claim_requested"),)
        if intent == "unsupported_request":
            return (ReuseDecision("blocked", "", "permission_or_safety_boundary"),)
        results = self.store.results_for_topic(topic.topic_id if topic else None)
        candidates = []
        for result in results:
            if not result.payload:
                continue
            try:
                validate_result_reuse_candidate(result.payload)
            except ValueError:
                continue
            candidates.append(result)
        if not candidates:
            if results:
                legacy = results[0]
                if contract_version != legacy.contract_version:
                    return (
                        ReuseDecision(
                            "context_only",
                            legacy.result_ref,
                            "contract_version_mismatch",
                        ),
                    )
                legacy_decision = evaluate_reuse_candidate(
                    source_snapshot=legacy.snapshot_id,
                    current_snapshot=current_snapshot,
                    permission_match=_can_read_scope(role, legacy.permission_scope),
                    semantic_scope_match=not _must_rerun(message, intent, relation),
                    source_ref=legacy.result_ref,
                )
                if legacy_decision.decision == "candidate":
                    return (
                        ReuseDecision(
                            "none",
                            legacy.result_ref,
                            "legacy_result_context_only",
                            can_support_claim=False,
                            requires_rerun=False,
                        ),
                    )
                return (legacy_decision,)
            return (ReuseDecision("rerun", "", "no_prior_result_ref"),)
        decisions: list[ReuseDecision] = []
        rejected: list[ReuseDecision] = []
        for candidate in candidates:
            if contract_version != candidate.contract_version:
                rejected.append(
                    ReuseDecision(
                        "context_only",
                        candidate.result_ref,
                        "contract_version_mismatch",
                    )
                )
                continue
            decision = evaluate_reuse_candidate(
                source_snapshot=candidate.snapshot_id,
                current_snapshot=current_snapshot,
                permission_match=_can_read_scope(role, candidate.permission_scope),
                semantic_scope_match=not _must_rerun(message, intent, relation),
                source_ref=candidate.result_ref,
            )
            if decision.decision == "candidate":
                decisions.append(decision)
            else:
                rejected.append(decision)
        semantic_reruns = [
            decision
            for decision in rejected
            if decision.decision == "rerun"
            and decision.reason == "semantic_scope_mismatch"
        ]
        return tuple(decisions or semantic_reruns or rejected[:1])

    def _reuse_candidate_payloads(
        self,
        topic: Optional[TopicState],
        reuse_decisions: tuple[ReuseDecision, ...],
    ) -> tuple[Mapping[str, Any], ...]:
        candidate_refs = {
            decision.result_ref
            for decision in reuse_decisions
            if decision.decision == "candidate" and decision.result_ref
        }
        if not topic or not candidate_refs:
            return ()
        return tuple(
            dict(record.payload)
            for record in self.store.results_for_topic(topic.topic_id)
            if record.result_ref in candidate_refs and record.payload
        )

    def _material_context_candidate_payloads(
        self,
        topic: Optional[TopicState],
        reuse_decisions: tuple[ReuseDecision, ...],
    ) -> tuple[Mapping[str, Any], ...]:
        material_refs = {
            decision.result_ref
            for decision in reuse_decisions
            if decision.result_ref
            and (
                decision.decision == "candidate"
                or (
                    decision.decision == "rerun"
                    and decision.reason == "semantic_scope_mismatch"
                )
            )
        }
        if not topic or not material_refs:
            return ()
        return tuple(
            dict(record.payload)
            for record in self.store.results_for_topic(topic.topic_id)
            if record.result_ref in material_refs and record.payload
        )

    def _validated_prior_topic_material_context(
        self,
        *,
        thread_id: str,
        topic: TopicState,
        role: str,
        candidates: tuple[Mapping[str, Any], ...],
    ) -> dict[str, Any]:
        resolve_candidate = getattr(
            self.store,
            "resolve_result_candidate_authority",
            None,
        )
        resolve_completed = getattr(
            self.store,
            "resolve_completed_material_authority",
            None,
        )
        if not callable(resolve_candidate) or not callable(resolve_completed):
            raise EvidenceIntegrityError(
                "prior_topic_authority_resolver_missing"
            )
        authorities_by_run: dict[str, dict[str, Any]] = {}
        result_refs: list[str] = []
        for raw_candidate in candidates:
            candidate = validate_result_reuse_candidate(raw_candidate)
            result_ref = candidate["result_ref"]
            indexed = resolve_candidate(
                result_ref=result_ref,
                topic_id=topic.topic_id,
            )
            indexed_record = indexed.get("result_ref_record")
            expected_indexed_record = {
                "topic_id": topic.topic_id,
                "result_ref": result_ref,
                "snapshot_id": candidate["runtime_snapshot_id"],
                "contract_version": candidate["runtime_contract_version"],
                "permission_scope": candidate["permission_scope"],
                "semantic_scope": candidate["semantic_scope_signature"],
                "payload": candidate,
            }
            if (
                not isinstance(indexed_record, Mapping)
                or canonical_value(indexed_record)
                != canonical_value(expected_indexed_record)
            ):
                raise EvidenceIntegrityError(
                    "prior_topic_result_candidate_authority_mismatch"
                )
            source_run_id = candidate["source_run_id"]
            if (
                str(indexed.get("source_run_id") or "") != source_run_id
                or str(indexed.get("run_status") or "") != "completed"
                or str(indexed.get("run_thread_id") or "") != thread_id
                or str(indexed.get("run_topic_id") or "")
                != topic.topic_id
            ):
                raise EvidenceIntegrityError(
                    "prior_topic_result_candidate_source_invalid"
                )
            completed = resolve_completed(
                source_run_id=source_run_id,
                thread_id=thread_id,
                topic_id=topic.topic_id,
            )
            contract = completed.get("analysis_contract")
            indexed_contract = indexed.get("analysis_contract")
            material = completed.get("material_authority")
            execution_material = (
                material.get("execution_material")
                if isinstance(material, Mapping)
                else None
            )
            indexed_contract_payload = (
                dict(indexed_contract)
                if isinstance(indexed_contract, Mapping)
                else {}
            )
            indexed_embedded_signature = str(
                indexed_contract_payload.pop("contract_signature", "") or ""
            )
            if not isinstance(contract, Mapping) or not isinstance(
                execution_material,
                Mapping,
            ):
                raise EvidenceIntegrityError(
                    "prior_topic_result_candidate_contract_mismatch"
                )
            permission_scopes = {
                candidate["permission_scope"],
                str(indexed_record.get("permission_scope") or ""),
                str(contract.get("permission_scope") or ""),
                str(execution_material.get("permission_scope") or ""),
            }
            if len(permission_scopes) != 1 or "" in permission_scopes:
                raise EvidenceIntegrityError(
                    "prior_topic_permission_scope_mismatch"
                )
            permission_scope = next(iter(permission_scopes))
            if not _can_read_scope(role, permission_scope):
                raise EvidenceIntegrityError(
                    "prior_topic_permission_scope_denied"
                )
            if (
                not indexed_contract_payload
                or canonical_value(indexed_contract_payload)
                != canonical_value(contract)
                or candidate["analysis_contract_ref"]
                != str(contract.get("analysis_contract_id") or "")
                or candidate["analysis_contract_signature"]
                != str(completed.get("analysis_contract_signature") or "")
                or candidate["analysis_contract_signature"]
                != str(
                    indexed.get("stored_analysis_contract_signature") or ""
                )
                or (
                    indexed_embedded_signature
                    and indexed_embedded_signature
                    != candidate["analysis_contract_signature"]
                )
            ):
                raise EvidenceIntegrityError(
                    "prior_topic_result_candidate_contract_mismatch"
                )
            canonical_completed = canonical_value(completed)
            existing = authorities_by_run.get(source_run_id)
            if existing is not None and existing != canonical_completed:
                raise EvidenceIntegrityError(
                    "prior_topic_completed_authority_conflict"
                )
            authorities_by_run[source_run_id] = canonical_completed
            result_refs.append(result_ref)
        return build_prior_topic_material_context(
            thread_id=thread_id,
            topic_id=topic.topic_id,
            source_result_refs=result_refs,
            authorities=authorities_by_run.values(),
        )

    def _context_manifest(
        self,
        thread_id: str,
        turn_id: str,
        topic: Optional[TopicState],
        message: str,
        role: str,
        current_snapshot: str,
        contract_version: str,
        reuse_decisions: tuple[ReuseDecision, ...],
        owner_scope: str,
        analysis_assets: tuple[dict[str, Any], ...],
        pending_clarification_id: str = "",
        prior_topic_material_context: Mapping[str, Any] | None = None,
        intent: str = "",
        accepted_assumptions: tuple[Mapping[str, Any], ...] = (),
    ) -> ContextManifest:
        items: list[ContextItem] = []
        if pending_clarification_id:
            items.append(
                ContextItem(
                    source_type="clarification",
                    source_ref=pending_clarification_id,
                    summary="用户已回答上一轮澄清问题，本轮按该选择恢复执行。",
                    can_support_claims=False,
                    reason="clarification_outcome",
                    permission_scope=role,
                    source_version=contract_version,
                    claim_use="context_only",
                )
            )
        if topic:
            items.append(
                ContextItem(
                    source_type="topic",
                    source_ref=topic.topic_id,
                    summary=topic.summary,
                    can_support_claims=False,
                    reason="topic_context_only",
                    permission_scope=role,
                    source_version=contract_version,
                    claim_use="context_only",
                )
            )
        for authority in (
            prior_topic_material_context or {}
        ).get("authorities", ()):
            if not isinstance(authority, Mapping):
                continue
            source_run_id = str(authority.get("source_run_id") or "")
            items.append(
                ContextItem(
                    source_type="material_authority",
                    source_ref=(
                        f"completed-material-authority:{source_run_id}"
                    ),
                    summary="已验证的上一轮业务分析物料，仅用于续问意图绑定。",
                    can_support_claims=False,
                    reason="prior_topic_material_context",
                    permission_scope=str(
                        (prior_topic_material_context or {}).get(
                            "permission_scope"
                        )
                        or ""
                    ),
                    source_version=str(
                        authority.get("analysis_contract_signature") or ""
                    ),
                    claim_use="context_only",
                )
            )
        for decision in reuse_decisions:
            if not decision.result_ref:
                continue
            items.append(
                ContextItem(
                    source_type="result_ref",
                    source_ref=decision.result_ref,
                    summary=f"上一轮结果引用，当前复用判断为 {decision.decision}。",
                    can_support_claims=decision.decision == "reuse",
                    reason=decision.reason,
                    permission_scope=role,
                    source_version=f"{contract_version}:{current_snapshot}",
                    expired=decision.decision in {"rerun", "context_only", "blocked"},
                    claim_use=(
                        "context_only"
                        if decision.decision == "candidate"
                        else decision.decision
                    ),
                )
            )
        artifact = self.store.latest_artifact_for_topic(topic.topic_id if topic else None)
        if artifact and intent == "artifact_continue":
            artifact_can_support = (
                artifact.snapshot_id == current_snapshot
                and _can_read_scope(role, artifact.permission_scope)
            )
            items.append(
                ContextItem(
                    source_type="artifact",
                    source_ref=artifact.artifact_id,
                    summary=artifact.follow_up_context,
                    can_support_claims=artifact_can_support,
                    visibility=artifact.permission_scope,
                    reason="artifact_follow_up_context" if artifact_can_support else "artifact_context_only",
                    permission_scope=artifact.permission_scope,
                    source_version=artifact.snapshot_id,
                    expired=not artifact_can_support,
                    claim_use="reuse" if artifact_can_support else "context_only",
                )
            )
        for memory in self.store.long_term_memory(owner_scope):
            if role != "business_reader" or memory.visibility == "business_reader":
                items.append(
                    ContextItem(
                        source_type="memory",
                        source_ref=memory.source_ref,
                        summary=memory.text,
                        can_support_claims=False,
                        visibility=memory.visibility,
                        reason="preference_only",
                        permission_scope=memory.visibility,
                        source_version=memory.ttl,
                        claim_use="preference_only",
                    )
                )
        if not items:
            items.append(
                ContextItem(
                    source_type="policy",
                    source_ref="conversation-boundary",
                    summary="本轮没有可复用 BI 证据上下文。",
                    can_support_claims=False,
                    reason="no_context",
                    permission_scope=role,
                    source_version=contract_version,
                    claim_use="context_only",
                )
            )
        claim_safe = all(
            decision.decision not in {"rerun", "blocked", "context_only"}
            for decision in reuse_decisions
        )
        has_claim_support = any(item.can_support_claims for item in items)
        artifact_context_blocked = any(
            item.source_type == "artifact" and not item.can_support_claims
            for item in items
        )
        return ContextManifest(
            manifest_id=f"context-{uuid4().hex[:12]}",
            thread_id=thread_id,
            turn_id=turn_id,
            topic_id=topic.topic_id if topic else None,
            items=tuple(items),
            claim_use_policy={
                "requires_evidence_ref": True,
                "can_support_bi_claim": has_claim_support and claim_safe and not artifact_context_blocked,
            },
            snapshot_version=current_snapshot,
            permission_context={"role": role},
            analysis_assets=list(analysis_assets),
            accepted_assumptions=[dict(item) for item in accepted_assumptions],
            contract_versions={"runtime": contract_version},
            schema_fingerprint=f"{contract_version}:{current_snapshot}",
            can_support_claims=has_claim_support and claim_safe and not artifact_context_blocked,
        )

    def _memory_proposals(
        self,
        thread_id: str,
        turn_id: str,
        message: str,
        intent: str,
        owner_scope: str,
        role: str,
    ) -> tuple[MemoryProposal, ...]:
        if intent != "memory_update":
            return ()
        action = "撤销" if "删掉" in message else "默认把 WajeSpecial 单独观察"
        return (
            MemoryProposal(
                proposal_id=f"memory-proposal-{uuid4().hex[:12]}",
                thread_id=thread_id,
                text=action,
                source_ref=turn_id,
                owner_scope=owner_scope,
                visibility=role,
            ),
        )

    def _topic_analysis_assets(
        self,
        thread_id: str,
        topic: Optional[TopicState],
    ) -> tuple[dict[str, Any], ...]:
        if not topic or not hasattr(self.store, "list_analysis_assets"):
            return ()
        return tuple(self.store.list_analysis_assets(thread_id, topic.topic_id))


def evaluate_reuse_candidate(
    *,
    source_snapshot: str | None,
    current_snapshot: str | None,
    permission_match: bool,
    semantic_scope_match: bool,
    source_ref: str = "candidate",
) -> ReuseDecision:
    if not permission_match:
        return ReuseDecision(
            "blocked",
            source_ref,
            "permission_scope_mismatch",
            can_support_claim=False,
            requires_rerun=True,
        )
    if source_snapshot != current_snapshot:
        return ReuseDecision(
            "context_only",
            source_ref,
            "snapshot_mismatch",
            can_support_claim=False,
            requires_rerun=True,
        )
    if not semantic_scope_match:
        return ReuseDecision(
            "rerun",
            source_ref,
            "semantic_scope_mismatch",
            can_support_claim=False,
            requires_rerun=True,
        )
    return ReuseDecision(
        "candidate",
        source_ref,
        "candidate_same_thread_scope",
        can_support_claim=False,
        requires_rerun=False,
    )
def _local_orchestration(intent: str, topic_relation: str, message: str) -> dict[str, Any]:
    return {
        "intent": intent,
        "topic_relation": topic_relation,
        "confidence": 0.82,
        "decision_source": "local_conversation_orchestrator",
        "business_summary": _intent_summary(intent, message),
    }


def _should_use_llm_orchestrator(
    local_intent: str,
    local_topic_relation: str,
    message: str,
    allow_clarification_answer: bool,
) -> bool:
    if allow_clarification_answer:
        return False
    if local_intent in LOCAL_GUARDED_INTENTS:
        return True
    if local_topic_relation in {
        "ask_topic_choice",
        "select_referenced_topic",
        "split_topics",
        "split_subintents",
        "queued_new_topic",
    }:
        return True
    if local_intent in {"mixed_question", "capability_question", "memory_update"}:
        return True
    if local_intent == "challenge":
        return True
    return False


def _validated_orchestration(
    output: Any,
    local: dict[str, Any],
    *,
    allow_clarification_answer: bool,
    active_run_status: str,
    topic_count: int,
) -> dict[str, Any]:
    if not isinstance(output, dict):
        raise ConversationOrchestrationError(
            "conversation_orchestrator_output_invalid"
        )

    intent = str(output.get("intent") or "").strip()
    topic_relation = str(output.get("topic_relation") or "").strip()
    if intent not in ALLOWED_INTENTS or topic_relation not in ALLOWED_TOPIC_RELATIONS:
        raise ConversationOrchestrationError(
            "conversation_orchestrator_output_invalid"
        )

    if local["intent"] in LOCAL_GUARDED_INTENTS and intent != local["intent"]:
        return _local_fallback(local, "local_conversation_orchestrator_guard")

    if intent == "clarification_answer" and not allow_clarification_answer:
        raise ConversationOrchestrationError(
            "conversation_orchestrator_output_invalid"
        )

    if intent in {"off_topic", "unsupported_request"}:
        topic_relation = "rejected"
    elif intent == "capability_question" and topic_relation not in {"inherit_current", "rejected"}:
        topic_relation = "rejected"
    elif intent == "memory_update":
        topic_relation = "inherit_current"
    elif active_run_status == "running" and intent == "new_topic":
        topic_relation = "queued_new_topic"
    elif topic_relation == "select_referenced_topic" and topic_count < 2:
        raise ConversationOrchestrationError(
            "conversation_orchestrator_output_invalid"
        )

    if topic_count == 0 and _should_run(intent, topic_relation):
        intent = "new_topic"
        topic_relation = "new_topic"

    business_summary = output.get("business_summary")
    if not isinstance(business_summary, str) or not business_summary.strip():
        raise ConversationOrchestrationError(
            "conversation_orchestrator_business_summary_invalid"
        )

    return {
        "intent": intent,
        "topic_relation": topic_relation,
        "confidence": _confidence(output.get("confidence")),
        "decision_source": "llm_conversation_orchestrator",
        "business_summary": business_summary.strip(),
    }


def _local_fallback(local: dict[str, Any], source: str) -> dict[str, Any]:
    fallback = dict(local)
    fallback["decision_source"] = source
    return fallback


def _confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.7
    return max(0.0, min(1.0, confidence))


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _has_all_concepts(text: str, *concepts: tuple[str, ...]) -> bool:
    return all(_has_any(text, concept) for concept in concepts)


_WRITE_CAPABILITY_ROOTS = (
    "下发",
    "发放",
    "发送",
    "推送",
    "投放",
    "触达",
    "通知",
    "赠送",
    "派发",
    "派送",
)
_WRITE_OBJECT_CONTRACT = (
    "优惠券",
    "券",
    "奖励",
    "消息",
    "通知",
    "短信",
    "权益",
    "福利",
    "提醒",
    "公告",
    "站内信",
)
_WRITE_COMMUNICATION_OBJECT_CONTRACT = (
    "提醒",
    "公告",
    "站内信",
    "消息",
    "通知",
    "短信",
    "邮件",
)
_WRITE_EXECUTION_GOVERNORS = ("执行", "安排")
_WRITE_CURRENT_MODALS = (
    "现在",
    "马上",
    "立即",
    "立刻",
    "帮我",
    "帮忙",
    "麻烦",
    "执行",
    "安排",
)
_WRITE_ANALYTIC_GOVERNORS = (
    "分析",
    "统计",
    "研究",
    "查看",
    "看一下",
    "回看",
    "复盘",
    "回顾",
    "评估",
    "比较",
    "对比",
    "检查",
    "核对",
)
_WRITE_HISTORY_ANCHORS = (
    "昨天",
    "前天",
    "上周",
    "上个月",
    "上月",
    "去年",
    "此前",
    "先前",
    "曾经",
    "历史上",
    "刚才",
)
_WRITE_STATISTICAL_HEADS = (
    "人数",
    "比例",
    "情况",
    "变化",
    "表现",
    "规模",
    "次数",
    "覆盖",
    "效果",
    "影响",
    "趋势",
    "率",
    "量",
    "数",
)
_WRITE_INTERROGATIVES = ("多少", "如何", "怎么样", "是否", "为什么")


def _regex_terms(terms: tuple[str, ...]) -> str:
    return "(?:" + "|".join(
        re.escape(term) for term in sorted(terms, key=len, reverse=True)
    ) + ")"


_WRITE_OBJECT = _regex_terms(_WRITE_OBJECT_CONTRACT)
_WRITE_COMMUNICATION_OBJECT = _regex_terms(
    _WRITE_COMMUNICATION_OBJECT_CONTRACT
)
_WRITE_BARE_FA_LIGHT = r"(?:了|过|一下)?"
_WRITE_BARE_FA_DIRECTIONAL_COMPLEMENT = (
    rf"{_WRITE_BARE_FA_LIGHT}(?:给|到|往|至)"
)
_WRITE_BARE_FA_COMMUNICATION_PAYLOAD = (
    rf"{_WRITE_BARE_FA_LIGHT}[\u4e00-\u9fff]{{0,4}}"
    rf"{_WRITE_COMMUNICATION_OBJECT}"
)
_WRITE_BARE_FA_COMPLEMENT = (
    rf"(?:{_WRITE_BARE_FA_DIRECTIONAL_COMPLEMENT}|"
    rf"{_WRITE_BARE_FA_COMMUNICATION_PAYLOAD}|"
    rf"{_WRITE_BARE_FA_LIGHT}(?:[0-9零一二三四五六七八九十百千万两几]+|"
    rf"(?:一|两|几)?(?:封|张|条|份|批|个)|电子邮件|邮件|{_WRITE_OBJECT})"
    rf")"
)
_WRITE_PREDICATE = (
    rf"(?:{_regex_terms(_WRITE_CAPABILITY_ROOTS)}|"
    rf"发(?={_WRITE_BARE_FA_COMPLEMENT}))"
)
_WRITE_DIRECT_TARGET = rf"(?:{_WRITE_OBJECT}|电子邮件|邮件)"
_WRITE_ANALYTIC = _regex_terms(_WRITE_ANALYTIC_GOVERNORS)
_WRITE_STATISTICAL_HEAD = _regex_terms(_WRITE_STATISTICAL_HEADS)
_WRITE_INTERROGATIVE = _regex_terms(_WRITE_INTERROGATIVES)
_WRITE_RECIPIENT = r"(?:给|向|为|对(?!比|照|于))"
_WRITE_CLAUSE_BODY = r"[^，,；;。！？!?:：\r\n]*"
_WRITE_PREDICATE_RE = re.compile(_WRITE_PREDICATE)
_WRITE_RECIPIENT_PREDICATE_RE = re.compile(
    rf"{_WRITE_RECIPIENT}{_WRITE_CLAUSE_BODY}{_WRITE_PREDICATE}"
)
_WRITE_DIRECT_REQUEST_RE = re.compile(
    rf"^\s*(?:请(?:你|您)?\s*)?"
    rf"{_WRITE_PREDICATE}{_WRITE_CLAUSE_BODY}{_WRITE_DIRECT_TARGET}"
)
_WRITE_BARE_FA_TRANSFER_RE = re.compile(
    rf"(?:^\s*请(?:你|您)?\s*发(?={_WRITE_BARE_FA_DIRECTIONAL_COMPLEMENT})|"
    rf"(?:把|将){_WRITE_CLAUSE_BODY}{_WRITE_DIRECT_TARGET}"
    rf"{_WRITE_CLAUSE_BODY}发(?={_WRITE_BARE_FA_DIRECTIONAL_COMPLEMENT}))"
)
_WRITE_SEQUENCE_RE = re.compile(
    rf"{_WRITE_ANALYTIC}{_WRITE_CLAUSE_BODY}"
    rf"(?:然后|随后|同时|接着|并且|再|并|之后|后|"
    rf"完{_WRITE_CLAUSE_BODY}(?:就|便|随即))"
    rf"\s*(?:{_WRITE_RECIPIENT}{_WRITE_CLAUSE_BODY})?"
    rf"{_WRITE_PREDICATE}"
)
_WRITE_RECIPIENT_NOMINAL_RE = re.compile(
    rf"{_WRITE_RECIPIENT}{_WRITE_CLAUSE_BODY}的{_WRITE_CLAUSE_BODY}"
    rf"{_WRITE_PREDICATE}{_WRITE_CLAUSE_BODY}{_WRITE_STATISTICAL_HEAD}"
)
_WRITE_NOMINAL_QUERY_RE = re.compile(
    rf"{_WRITE_PREDICATE}{_WRITE_CLAUSE_BODY}{_WRITE_STATISTICAL_HEAD}"
    rf"{_WRITE_CLAUSE_BODY}{_WRITE_INTERROGATIVE}"
)
_WRITE_EVENT_REFERENCE_RE = re.compile(
    rf"{_WRITE_PREDICATE}{_WRITE_CLAUSE_BODY}"
    rf"(?:之后的|后的|前后|期间|以来|实验(?!组)|的{_WRITE_STATISTICAL_HEAD})"
)
_WRITE_POLITE_PREFIX = r"(?:(?:请(?:你|您)?|帮我|帮忙|麻烦(?:你|您)?)\s*)?"
_WRITE_CLAUSE_ANALYTIC_RE = re.compile(
    rf"^\s*{_WRITE_POLITE_PREFIX}{_WRITE_ANALYTIC}{_WRITE_CLAUSE_BODY}"
    rf"{_WRITE_RECIPIENT}{_WRITE_CLAUSE_BODY}{_WRITE_PREDICATE}"
)
_WRITE_MATERIAL_REFERENCE_RE = re.compile(
    rf"^\s*{_WRITE_POLITE_PREFIX}(?:基于|根据|按照|结合)"
    rf"{_WRITE_CLAUSE_BODY}分析(?:结果|结论|报告|数据)"
    rf"{_WRITE_CLAUSE_BODY}{_WRITE_RECIPIENT}"
    rf"{_WRITE_CLAUSE_BODY}{_WRITE_PREDICATE}"
)
_WRITE_ANALYTIC_REFERENCE_RE = re.compile(
    rf"{_WRITE_ANALYTIC}{_WRITE_CLAUSE_BODY}{_WRITE_PREDICATE}"
)
_WRITE_QUOTE_RE = re.compile(
    r"""“([^”]*)”|‘([^’]*)’|"([^"]*)"|'([^']*)'|"""
    r"""「([^」]*)」|『([^』]*)』|《([^》]*)》"""
)


def _last_term_position(text: str, terms: tuple[str, ...]) -> int:
    return max((text.rfind(term) for term in terms), default=-1)


def _nearest_quote_governor(text: str, match: re.Match[str]) -> str | None:
    prefix = re.split(r"[，,；;。！？!?:：\r\n]", text[: match.start()])[-1]
    suffix = re.split(r"[，,；;。！？!?:：\r\n]", text[match.end() :])[0]
    candidates: list[tuple[int, int, str]] = []
    for kind, terms in (
        ("execute", _WRITE_EXECUTION_GOVERNORS),
        ("analyze", _WRITE_ANALYTIC_GOVERNORS),
    ):
        for term in terms:
            before = prefix.rfind(term)
            if before >= 0:
                candidates.append(
                    (len(prefix) - before - len(term), kind != "execute", kind)
                )
            after = suffix.find(term)
            if after >= 0:
                suffix_kind = kind
                if kind == "execute" and re.search(
                    rf"{_WRITE_CLAUSE_BODY}"
                    rf"(?:{_WRITE_STATISTICAL_HEAD}|{_WRITE_INTERROGATIVE})",
                    suffix[after + len(term) :],
                ):
                    suffix_kind = "analyze"
                candidates.append(
                    (after, suffix_kind != "execute", suffix_kind)
                )
    return min(candidates)[2] if candidates else None


def _quoted_write_is_current(text: str) -> bool:
    for match in _WRITE_QUOTE_RE.finditer(text):
        content = next(group for group in match.groups() if group is not None)
        if _WRITE_PREDICATE_RE.search(content) is None:
            continue
        if _nearest_quote_governor(text, match) == "execute":
            return True
    return False


def _without_quoted_segments(text: str) -> str:
    return _WRITE_QUOTE_RE.sub(
        lambda match: " " * (match.end() - match.start()),
        text,
    )


def _write_predicate_has_current_modal(
    clause: str,
    predicate: re.Match[str],
) -> bool:
    prefix = clause[: predicate.start()]
    modal = _last_term_position(prefix, _WRITE_CURRENT_MODALS)
    analytic = _last_term_position(clause, _WRITE_ANALYTIC_GOVERNORS)
    return modal > analytic


def _write_predicate_is_historical(
    clause: str,
    predicate: re.Match[str],
) -> bool:
    prefix = clause[: predicate.start()]
    analytic = _last_term_position(prefix, _WRITE_ANALYTIC_GOVERNORS)
    anchor = _last_term_position(prefix, _WRITE_HISTORY_ANCHORS)
    completed_prefix = _last_term_position(
        prefix,
        ("已经", "已", "曾经", "曾"),
    )
    completed_suffix = clause[predicate.end() :].lstrip().startswith(("了", "过"))
    return analytic < 0 and (
        anchor >= 0 or completed_prefix >= 0 or completed_suffix
    )


def _write_clause_is_current(clause: str) -> bool:
    predicates = tuple(_WRITE_PREDICATE_RE.finditer(clause))
    bare_fa_transfer = _WRITE_BARE_FA_TRANSFER_RE.search(clause) is not None
    if not predicates:
        return bare_fa_transfer
    if any(
        _write_predicate_has_current_modal(clause, predicate)
        for predicate in predicates
    ):
        return True
    if _WRITE_SEQUENCE_RE.search(clause):
        return True
    if all(
        _write_predicate_is_historical(clause, predicate)
        for predicate in predicates
    ):
        return False
    if bare_fa_transfer:
        return True
    if (
        _WRITE_RECIPIENT_NOMINAL_RE.search(clause)
        or _WRITE_NOMINAL_QUERY_RE.search(clause)
        or _WRITE_EVENT_REFERENCE_RE.search(clause)
    ):
        return False
    if _WRITE_MATERIAL_REFERENCE_RE.search(clause):
        return True
    if _WRITE_CLAUSE_ANALYTIC_RE.search(clause):
        return False
    if _WRITE_RECIPIENT_PREDICATE_RE.search(clause):
        return True
    if _WRITE_ANALYTIC_REFERENCE_RE.search(clause):
        return False
    return bool(
        _WRITE_DIRECT_REQUEST_RE.search(clause)
        or _WRITE_BARE_FA_TRANSFER_RE.search(clause)
    )


def _is_write_action_request(text: str) -> bool:
    if _quoted_write_is_current(text):
        return True
    outside_quotes = _without_quoted_segments(text)
    return any(
        _write_clause_is_current(clause)
        for clause in re.split(r"[，,；;。！？!?:：\r\n]+", outside_quotes)
        if clause.strip()
    )


def _is_unsupported_request(text: str) -> bool:
    raw_identifier = _has_all_concepts(
        text,
        ("用户 ID", "用户ID", "用户标识", "设备 ID", "设备ID", "IP"),
        ("原始", "明细", "逐条", "导出", "列出"),
    )
    raw_sql = _has_all_concepts(
        text.upper(),
        ("SQL", "查询语句"),
        ("直接", "执行", "运行", "写", "查询"),
    )
    write_action = _is_write_action_request(text)
    forecast = _has_all_concepts(
        text,
        ("预测", "预估", "预判", "推演"),
        ("下个月", "未来", "明年", "下一季度"),
    )
    return raw_identifier or raw_sql or write_action or forecast


def _is_off_topic_request(text: str) -> bool:
    food_request = _has_all_concepts(
        text,
        ("吃", "午饭", "晚饭", "餐"),
        ("什么", "推荐", "选择"),
    )
    creative_request = _has_all_concepts(
        text,
        ("写", "创作", "生成"),
        ("诗", "故事", "小说"),
    )
    return food_request or creative_request


def _is_capability_question(text: str) -> bool:
    data_visibility = _has_all_concepts(
        text,
        ("能", "可以", "支持", "可用"),
        ("数据", "字段", "来源", "维度"),
    )
    supported_analysis = _has_all_concepts(
        text,
        ("能", "可以", "支持"),
        ("分析", "拆解", "组合"),
        ("按", "维度", "渠道", "支付方式"),
    )
    proof_boundary = _has_all_concepts(
        text,
        ("为什么", "为何"),
        ("不能", "无法", "不支持"),
        ("证明", "因果", "归因"),
    )
    external_access = _has_all_concepts(
        text,
        ("联网", "新闻", "外部信息", "外部数据"),
        ("能", "会", "可以", "支持"),
    )
    sharing_visibility = _is_sharing_visibility_question(text)
    return (
        data_visibility
        or supported_analysis
        or proof_boundary
        or external_access
        or sharing_visibility
    )


def _has_material_analysis_objective(text: str) -> bool:
    has_change_objective = _has_any(
        text,
        (
            "变化",
            "变差",
            "改善",
            "上涨",
            "下降",
            "增加",
            "减少",
            "趋势",
            "表现",
            "波动",
            "提升",
            "回落",
        ),
    )
    return (
        (
            _looks_new_topic(text)
            or bool(_mentioned_metrics(text)) and has_change_objective
        )
        and _has_all_concepts(
            text,
            ("数据", "字段", "证据", "材料"),
            ("支持", "能", "可以", "到哪", "边界"),
        )
    )


def _is_sharing_visibility_question(text: str) -> bool:
    return _has_all_concepts(
        text,
        ("分享", "转给", "给老板", "给管理层"),
        ("看到", "可见", "展示", "能看"),
    )


def _is_memory_request(text: str) -> bool:
    return _has_all_concepts(
        text,
        ("默认", "偏好", "习惯", "记忆"),
        ("以后", "记住", "保存", "删除", "删掉", "撤销"),
    )


def _is_artifact_continuation(text: str) -> bool:
    return _has_all_concepts(
        text,
        ("结果", "结论", "报告", "保存"),
        ("基于", "继续", "打开", "接着", "重新查看"),
    )


def _is_correction_request(text: str) -> bool:
    explicit_revision = _has_any(
        text,
        (
            "改成",
            "改为",
            "换成",
            "调整为",
            "调整到",
            "切换为",
            "切到",
            "不再按",
            "说错",
            "纠正",
            "改看",
            "改用",
        ),
    )
    binding_context = _has_any(
        text,
        (
            "口径",
            "指标",
            "基线",
            "窗口",
            "粒度",
            "范围",
            "维度",
            "统计方式",
            "计算方式",
            "总金额",
            "总额",
            "日均",
            "每位付费用户",
            "自然月",
            "活动前后",
            "按周",
            "按日",
        ),
    )
    source_to_target = binding_context and bool(
        re.search(
            r"(?:由|从).{1,32}(?:改成|改为|调整为|调整到|切换为|切到|到).{1,32}",
            text,
        )
    )
    negative_binding = bool(
        re.search(r"(?:不要|别再|不再|别)\s*(?:按|看|用|采用)", text)
    )
    replacement_binding = bool(
        re.search(
            r"(?:换成|调整为|调整到|切换为|切到|改看|改用|"
            r"[，,；;]\s*(?:改?按|改?看|改用|采用|用|按|看))",
            text,
        )
    )
    return (
        explicit_revision
        or source_to_target
        or (negative_binding and replacement_binding)
    )


def _is_data_freshness_request(text: str) -> bool:
    return _has_all_concepts(
        text,
        ("数据更新", "最新数据", "新快照", "刷新后", "更新后"),
        ("还成立", "仍成立", "对比", "变化", "现在", "重新"),
    )


def _is_evidence_sufficiency_challenge(text: str) -> bool:
    return _has_all_concepts(
        text,
        ("证据", "材料", "数据"),
        ("足以", "足够", "充分", "够不够", "能否支撑", "可以支撑"),
    )


def _is_analysis_constraint_challenge(text: str) -> bool:
    negative_modal = bool(
        re.search(
            r"(?:先不要|不要|别|禁止|不得|避免|不应|(?<!能)不能|无需|不许)",
            text,
        )
    )
    causal_semantics = _has_any(
        text,
        ("因果", "归因", "导致", "造成", "认定", "断言", "解释为"),
    )
    capability_explanation = _has_all_concepts(
        text,
        ("为什么", "为何"),
        ("不能", "无法", "不支持"),
        ("证明", "因果", "归因"),
    )
    return (
        negative_modal
        and causal_semantics
        and not capability_explanation
    )


def _is_challenge(text: str) -> bool:
    if _is_outlier_removal_question(text):
        return True
    robustness = _has_all_concepts(
        text,
        ("结论", "判断", "结果", "方向"),
        ("稳", "可靠", "敏感", "经得起", "复核", "还成立", "仍成立"),
    )
    evidence_sufficiency = _is_evidence_sufficiency_challenge(text)
    causal_attribution = _has_all_concepts(
        text,
        ("归因", "导致", "造成", "因果"),
        ("能", "可以", "是否", "吗", "直接"),
    )
    interference = _has_all_concepts(
        text,
        ("干扰", "偏差", "误导", "带偏"),
        ("结论", "判断", "结果", "这个"),
    )
    decision_readiness = _has_all_concepts(
        text,
        ("指导", "执行", "采取", "用于"),
        ("投放", "决策", "运营动作", "活动"),
    )
    return (
        robustness
        or evidence_sufficiency
        or causal_attribution
        or interference
        or decision_readiness
    )


def _classify_intent(message: str, allow_clarification_answer: bool) -> str:
    text = message.strip()
    if allow_clarification_answer:
        return "clarification_answer"
    if _is_analysis_constraint_challenge(text):
        return "challenge"
    if _is_unsupported_request(text):
        return "unsupported_request"
    if _is_off_topic_request(text):
        return "off_topic"
    if _is_memory_request(text):
        return "memory_update"
    if _is_artifact_continuation(text):
        return "artifact_continue"
    if _is_correction_request(text):
        return "correction"
    if _is_data_freshness_request(text):
        return "follow_up"
    if _is_mixed(text):
        return "mixed_question"
    if _has_material_analysis_objective(text):
        return "new_topic"
    if _is_capability_question(text):
        return "capability_question"
    if _is_challenge(text):
        return "challenge"
    if _looks_new_topic(text):
        return "new_topic"
    return "follow_up"


def _topic_relation(intent: str, message: str, active_run_status: str) -> str:
    if intent in {"off_topic", "unsupported_request"}:
        return "rejected"
    if intent == "capability_question":
        return "inherit_current" if _is_sharing_visibility_question(message) else "rejected"
    if _references_prior_topic_position(message):
        return "select_referenced_topic"
    if _has_ambiguous_prior_reference(message):
        return "ask_topic_choice"
    if active_run_status == "running" and intent == "new_topic":
        return "queued_new_topic"
    if intent == "mixed_question":
        objectives = _analysis_objectives(message)
        if "顺便" in message and "pattern" in objectives and _mentions_named_period(message):
            return "split_topics"
        if (
            (
                len(_mentioned_dimensions(message)) >= 2
                and _has_any(message, ("一起", "同时", "组合", "联合", "分别"))
            )
            or {"contribution", "data_quality"}.issubset(objectives)
            or "delivery" in objectives
        ):
            return "inherit_current"
        if _is_metric_health_question(message) or len(_mentioned_patterns(message)) >= 2:
            return "new_topic"
        return "split_subintents"
    if intent == "new_topic":
        return "new_topic"
    if intent == "correction" and _has_all_concepts(
        message,
        ("改看", "换看", "切换"),
        ("退款", "留存", "活跃", "新指标"),
    ):
        return "new_topic"
    if intent == "memory_update":
        return "inherit_current"
    return "inherit_current"


def _analysis_objectives(text: str) -> set[str]:
    objectives: set[str] = set()
    if _mentions_period_comparison(text) or _is_metric_health_question(text):
        objectives.add("comparison")
    if _has_all_concepts(
        text,
        ("渠道", "支付方式", "新用户", "老用户", "分群", "业务对象"),
        ("贡献", "影响", "拆解", "拉动", "拉低", "解释"),
    ):
        objectives.add("contribution")
    if _has_any(text, ("异常", "离群", "波峰", "极端日期")):
        objectives.add("outlier")
    if _has_any(text, ("原因", "为什么", "驱动", "解释变化", "归因")):
        objectives.add("driver")
    if _has_any(text, ("风险", "限制", "隐患")):
        objectives.add("risk")
    if _has_any(text, ("证明", "因果", "导致", "归因")):
        objectives.add("causal")
    if _has_all_concepts(
        text,
        ("活动", "事件"),
        ("前后", "窗口", "期间", "相对"),
    ):
        objectives.add("event_window")
    if _has_all_concepts(
        text,
        ("未知", "缺失", "完整", "质量"),
        ("渠道", "数据", "来源", "字段"),
    ):
        objectives.add("data_quality")
    if _has_any(text, ("老板", "管理层", "分享", "汇报")):
        objectives.add("delivery")
    if _has_any(text, ("还要观察", "下一步", "后续观察", "继续关注")):
        objectives.add("next_action")
    if _has_any(text, ("敏感", "稳健", "可靠", "经得起")):
        objectives.add("robustness")
    if _mentioned_patterns(text):
        objectives.add("pattern")
    return objectives


def _mentioned_dimensions(text: str) -> set[str]:
    dimensions: set[str] = set()
    for dimension, terms in {
        "channel": ("渠道",),
        "payment_method": ("支付方式", "支付渠道"),
        "user_tenure": ("新老用户", "新用户", "老用户"),
    }.items():
        if _has_any(text, terms):
            dimensions.add(dimension)
    return dimensions


def _mentioned_metrics(text: str) -> set[str]:
    return {
        metric_id
        for metric_id, _, _ in _metric_business_label_matches(text)
    }


@lru_cache(maxsize=1)
def _metric_business_label_vocabulary() -> Mapping[str, str]:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    return MappingProxyType(
        {
            label: metric_id
            for metric_id in registry.metric_ids
            for label in registry.metric_business_labels(metric_id)
        }
    )


def _metric_business_label_matches(text: str) -> tuple[tuple[str, int, int], ...]:
    occupied = [False] * len(text)
    matches: list[tuple[str, int, int]] = []
    vocabulary = _metric_business_label_vocabulary()
    for label in sorted(vocabulary, key=lambda item: (-len(item), item)):
        start = 0
        while (index := text.find(label, start)) >= 0:
            end = index + len(label)
            if not any(occupied[index:end]):
                matches.append((vocabulary[label], index, end))
                occupied[index:end] = [True] * len(label)
            start = end
    return tuple(matches)


def _mentioned_aggregation_scopes(text: str) -> set[str]:
    residual = list(text)
    for _, start, end in _metric_business_label_matches(text):
        residual[start:end] = " " * (end - start)
    residual_text = "".join(residual)

    return {
        scope
        for scope, terms in {
            "aggregation_total": ("总额", "总金额", "合计金额"),
            "aggregation_daily_average": ("日均", "每日平均"),
            "aggregation_order_count": ("订单数", "订单量", "笔数"),
        }.items()
        if _has_any(residual_text, terms)
    }


def _mentioned_patterns(text: str) -> set[str]:
    patterns: set[str] = set()
    for pattern, terms in {
        "month_start": ("月初",),
        "month_mid": ("月中",),
        "month_end": ("月末", "月底"),
        "weekend": ("周末",),
        "weekday": ("工作日",),
    }.items():
        if _has_any(text, terms):
            patterns.add(pattern)
    return patterns


def _is_mixed(text: str) -> bool:
    objectives = _analysis_objectives(text)
    connectors = _has_any(
        text,
        ("顺便", "一起", "同时", "并且", "并", "再看", "还要", "以及", "分别"),
    )
    if len(_mentioned_dimensions(text)) >= 2 and connectors:
        return True
    if len(_mentioned_metrics(text)) >= 2:
        return True
    if len(_mentioned_aggregation_scopes(text)) >= 2:
        return True
    if len(_mentioned_patterns(text)) >= 2 and _has_any(
        text, ("哪个更", "比较", "对比", "与", "和")
    ):
        return True
    inquiry_clauses = sum(
        text.count(term)
        for term in ("是否", "哪些", "有没有", "为什么", "能不能", "检查", "拆解")
    )
    return (connectors and len(objectives) >= 2) or (
        inquiry_clauses >= 2 and len(objectives) >= 2
    )


_PERIOD_COMPARISON_RE = re.compile(
    r"(?:(?:20\d{2}\s*年)?(?:第?[一二三四1-4]\s*季度|Q[1-4]))"
    r".{0,16}(?:相比|对比|比较|比|与|和|VS\.?)"
    r".{0,16}(?:(?:20\d{2}\s*年)?(?:第?[一二三四1-4]\s*季度|Q[1-4]))",
    re.IGNORECASE,
)


def _mentions_period_comparison(text: str) -> bool:
    if _PERIOD_COMPARISON_RE.search(text):
        return True
    return bool(
        re.search(
            r"\bQ[1-4]\b.{0,12}(?:变化|走势|表现|金额|收入|上涨|下降)",
            text,
            re.IGNORECASE,
        )
    )


def _mentions_named_period(text: str) -> bool:
    return bool(
        re.search(
            r"(?:\bQ[1-4]\b|第?[一二三四1-4]\s*季度|\d{1,2}\s*月)",
            text,
            re.IGNORECASE,
        )
    )


def _is_metric_health_question(text: str) -> bool:
    return _has_all_concepts(
        text,
        ("这个月", "本月", "最近一个月", "本期", "这段时间", "近期", "当前周期"),
        ("表现", "业绩", "经营", "结果", "整体", "业务", "变"),
        ("变好", "改善", "转好", "更健康", "好转", "回升"),
    )


def _references_prior_topic_position(text: str) -> bool:
    return bool(
        re.search(r"(?:刚才|之前|上面).{0,8}(?:第二|第2|另一个).{0,6}(?:问题|主题|分析)?", text)
    )


def _has_ambiguous_prior_reference(text: str) -> bool:
    return bool(
        re.search(r"(?:刚才|之前|上面).{0,8}(?:那个|哪个|哪一个)(?:问题|主题|分析)?", text)
    )


def _looks_new_topic(text: str) -> bool:
    if "刚才" in text:
        return False
    if _mentions_period_comparison(text):
        return True
    full_scope_pattern = _has_all_concepts(
        text,
        ("全样本", "全量", "全部数据", "历史覆盖", "完整样本"),
        ("月初", "月中", "月末", "月底", "月内阶段"),
    )
    explicit_new_request = _has_any(
        text,
        ("另外", "新问题", "换个问题", "再开一个", "另一个问题"),
    )
    standalone_domain_health = _has_all_concepts(
        text,
        ("留存", "退款", "活跃"),
        ("变差", "变化", "改善", "上涨", "下降"),
    )
    return (
        full_scope_pattern
        or explicit_new_request
        or standalone_domain_health
        or _is_metric_health_question(text)
    )


def _must_rerun(message: str, intent: str, relation: str) -> bool:
    if relation in {"new_topic", "queued_new_topic", "split_topics", "split_subintents"}:
        return True
    if intent in {"correction", "clarification_answer"}:
        return True
    if len(_mentioned_dimensions(message)) >= 2:
        return True
    if _has_all_concepts(
        message,
        ("每天", "每日", "逐日"),
        ("变化", "走势", "趋势", "图"),
    ):
        return True
    return _has_any(
        message,
        (
            "换成",
            "调整为",
            "只看",
            "过滤",
            "去掉",
            "剔除",
            "按周",
            "按天",
            "失败支付",
            "活动前后",
            "事件窗口",
            "日均",
            "每日平均",
            "数据更新",
            "最新数据",
            "新快照",
        ),
    )


def _needs_clarification(message: str) -> bool:
    ambiguous_outlier_strategy = (
        _is_outlier_removal_question(message)
        and not re.search(r"\d{1,2}\s*月\s*\d{1,2}\s*(?:日|号)", message)
    )
    return ambiguous_outlier_strategy or _is_metric_health_question(message)


def _build_clarification(
    turn_id: str,
    message: str,
    topic_relation: str,
) -> ClarificationRequest:
    if topic_relation == "ask_topic_choice":
        question = ClarificationQuestion(
            question_id="topic_reference",
            question="你想继续哪一个业务问题？",
            options=(
                ClarificationOption(
                    option_id="current_topic",
                    label="继续当前问题",
                    description="沿用当前打开的业务问题继续分析。",
                    recommended=True,
                ),
                ClarificationOption(
                    option_id="second_topic",
                    label="继续第二个问题",
                    description="切到 thread 里的第二条业务问题链。",
                ),
                ClarificationOption(
                    option_id="tell_agent_differently",
                    label=CLARIFICATION_ESCAPE_OPTION,
                    description="自己说明要继续哪个问题或换一个分析方式。",
                ),
            ),
        )
        return ClarificationRequest(
            clarification_id=f"clarification-{turn_id}",
            reason="topic_reference_ambiguous",
            questions=(question,),
        )

    if _is_outlier_removal_question(message):
        question = ClarificationQuestion(
            question_id="outlier_removal_strategy",
            question="你想按什么规则移除异常影响？",
            options=(
                ClarificationOption(
                    option_id="daily_remove_top_positive_day",
                    label="按日移除最大正向日",
                    description="先按天聚合，再移除贡献最大的正向日期后复算。",
                    recommended=True,
                ),
                ClarificationOption(
                    option_id="exclude_specific_dates",
                    label="指定日期再复算",
                    description="你自己指定要排除的异常日期范围。",
                ),
                ClarificationOption(
                    option_id="tell_agent_differently",
                    label=CLARIFICATION_ESCAPE_OPTION,
                    description="自己指定异常识别口径和剔除范围。",
                ),
            ),
        )
        return ClarificationRequest(
            clarification_id=f"clarification-{turn_id}",
            reason="outlier_removal_strategy_changes_business_answer",
            questions=(question,),
        )

    question = ClarificationQuestion(
        question_id="metric_and_baseline",
        question="你想用哪个口径判断“变好了”？",
        options=(
            ClarificationOption(
                option_id="daily_avg_paid_amount",
                label="按日均付费金额",
                description="更适合比较不同天数的时间窗口。",
                recommended=True,
            ),
            ClarificationOption(
                option_id="total_paid_amount",
                label="按付费总金额",
                description="适合判断整体收入规模变化。",
            ),
            ClarificationOption(
                option_id="tell_agent_differently",
                label=CLARIFICATION_ESCAPE_OPTION,
                description="自己指定指标、时间窗口或对比基线。",
            ),
        ),
    )
    return ClarificationRequest(
        clarification_id=f"clarification-{turn_id}",
        reason="metric_or_baseline_changes_business_answer",
        questions=(question,),
    )


def _looks_like_clarification_answer(
    text: str,
    clarification: ClarificationState,
) -> bool:
    normalized = text.strip().rstrip("。")
    option_texts = {
        part.strip().rstrip("。")
        for option in clarification.options
        for part in (option.option_id, option.label, option.description)
        if part and part.strip()
    }
    if normalized in option_texts:
        return True
    if _looks_new_topic(text) or _is_mixed(text):
        return False
    if normalized in {"按推荐继续", "推荐"}:
        return any(option.recommended for option in clarification.options)

    scope = " ".join(
        [clarification.question]
        + [
            f"{option.option_id} {option.label} {option.description}"
            for option in clarification.options
        ]
    )
    if "异常" in scope or "移除" in scope or "剔除" in scope:
        return _looks_like_outlier_clarification_answer(text)
    if "日均" in scope or "总金额" in scope or "口径" in scope:
        return _looks_like_metric_clarification_answer(normalized)
    return False


def _clarification_source_from_request(
    prior_request: Mapping[str, Any],
    *,
    source_run_id: str,
    source_thread_id: str,
    source_topic_id: str,
    source_owner_id: str,
) -> dict[str, Any]:
    raw_envelope = prior_request.get("clarification_source_envelope")
    if raw_envelope is None:
        raise ConversationOrchestrationError(
            "clarification_source_envelope_invalid"
        )
    if not isinstance(raw_envelope, Mapping):
        raise ConversationOrchestrationError(
            "clarification_source_envelope_invalid"
        )
    unsigned_envelope = dict(raw_envelope)
    source_digest = unsigned_envelope.pop("source_digest", None)
    expected_fields = {
        "schema_version",
        "source_run_id",
        "source_thread_id",
        "source_topic_id",
        "source_owner_id",
        "question",
        "analysis_context",
        "source_material",
        "clarification",
    }
    try:
        valid_source_digest = (
            isinstance(source_digest, str)
            and bool(source_digest)
            and source_digest == canonical_digest(unsigned_envelope)
        )
    except EvidenceIntegrityError:
        valid_source_digest = False
    source_material = raw_envelope.get("source_material")
    analysis_context = raw_envelope.get("analysis_context")
    clarification = raw_envelope.get("clarification")
    question = raw_envelope.get("question")
    if (
        raw_envelope.get("schema_version")
        != "clarification-source-envelope.v1"
        or set(unsigned_envelope) != expected_fields
        or not valid_source_digest
        or str(raw_envelope.get("source_run_id") or "") != source_run_id
        or str(raw_envelope.get("source_thread_id") or "")
        != source_thread_id
        or str(raw_envelope.get("source_topic_id") or "") != source_topic_id
        or str(raw_envelope.get("source_owner_id") or "") != source_owner_id
        or not isinstance(question, str)
        or not question
        or question != question.strip()
        or not isinstance(analysis_context, Mapping)
        or not isinstance(source_material, Mapping)
        or not isinstance(clarification, Mapping)
    ):
        raise ConversationOrchestrationError(
            "clarification_source_envelope_invalid"
        )
    accepted_graph = source_material.get("accepted_graph")
    material_mappings = (
        "analysis_contract",
        "analysis_route",
        "original_intent",
        "material_slots",
    )
    if (
        not isinstance(accepted_graph, (list, tuple))
        or any(
            not isinstance(source_material.get(key), Mapping)
            for key in material_mappings
        )
    ):
        raise ConversationOrchestrationError(
            "clarification_source_envelope_invalid"
        )
    return {
        "source_run_id": source_run_id,
        "source_thread_id": source_thread_id,
        "source_topic_id": source_topic_id,
        "question": question,
        "analysis_context": dict(analysis_context),
        "source_material": {
            "accepted_graph": list(accepted_graph),
            **{key: dict(source_material[key]) for key in material_mappings},
        },
        "clarification": dict(clarification),
    }


def _selected_query_gap_action(
    clarification: Mapping[str, Any],
    user_message: str,
    *,
    selected_option_id: str = "",
    clarification_state: ClarificationState | None = None,
) -> dict[str, Any]:
    actions = tuple(
        dict(action)
        for action in clarification.get("choice_actions") or ()
        if isinstance(action, Mapping)
    )
    normalized = user_message.strip().rstrip("。")

    def actions_for_label(label: str) -> tuple[dict[str, Any], ...]:
        return tuple(
            action
            for action in actions
            if clarification_labels_match(
                action.get("business_label")
                or action.get("business_semantics"),
                label,
            )
        )

    if selected_option_id:
        state_options = tuple(
            clarification_state.options if clarification_state else ()
        )
        selected_option = next(
            (
                option
                for option in state_options
                if option.option_id == selected_option_id
            ),
            None,
        )
        if selected_option is None:
            raise ConversationOrchestrationError(
                "clarification_selected_option_invalid"
            )
        direct = tuple(
            action
            for action in actions
            if str(action.get("choice_id") or "") == selected_option_id
        )
        if len(direct) == 1:
            return direct[0]
        if len(direct) > 1:
            raise ConversationOrchestrationError(
                "clarification_selected_option_conflict"
            )
        legacy = actions_for_label(selected_option.label)
        if len(legacy) == 1:
            return legacy[0]
        if len(legacy) > 1:
            raise ConversationOrchestrationError(
                "clarification_selected_option_conflict"
            )
        return {}

    exact = actions_for_label(normalized)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ConversationOrchestrationError(
            "clarification_selected_option_conflict"
        )
    if normalized not in {"按推荐继续", "推荐"}:
        return {}
    recommended_id = str(clarification.get("recommended_choice_id") or "")
    if not recommended_id:
        recommended = clarification.get("recommended_assumption") or {}
        recommended_label = str(
            recommended.get("option") if isinstance(recommended, Mapping) else recommended
        ).strip().rstrip("。")
        for action in actions:
            if clarification_labels_match(
                action.get("business_label")
                or action.get("business_semantics"),
                recommended_label,
            ):
                recommended_id = str(action.get("choice_id") or "")
                break
    recommended_actions = tuple(
        action
        for action in actions
        if str(action.get("choice_id") or "") == recommended_id
    )
    if len(recommended_actions) > 1:
        raise ConversationOrchestrationError(
            "clarification_recommended_option_conflict"
        )
    return recommended_actions[0] if recommended_actions else {}


def _looks_like_outlier_clarification_answer(text: str) -> bool:
    return (
        any(token in text for token in ("移除", "剔除", "排除", "去掉", "排掉"))
        and any(token in text for token in ("按日", "按天", "日期", "天", "日"))
        and any(token in text for token in ("复算", "贡献最大", "最大正向"))
    )


def _looks_like_metric_clarification_answer(text: str) -> bool:
    if any(token in text for token in ("为什么", "怎么", "多少", "变化", "掉了", "?", "？")):
        return False
    return text in {
        "日均",
        "按日均",
        "按日均付费金额",
        "付费总金额",
        "总金额",
        "按付费总金额",
    }


def _is_outlier_removal_question(text: str) -> bool:
    removal_tokens = ("移除", "剔除", "排除", "去掉", "排掉")
    outlier_tokens = ("异常", "波峰", "波动", "日期", "天", "日")
    date_selection = bool(
        re.search(r"\d{1,2}\s*月\s*\d{1,2}\s*(?:日|号)", text)
    )
    return _has_any(text, removal_tokens) and (
        _has_any(text, outlier_tokens) or date_selection
    )


def _should_run(intent: str, relation: str) -> bool:
    if relation == "ask_topic_choice":
        return False
    return intent not in {"off_topic", "capability_question", "unsupported_request", "memory_update"}


def _requested_nodes(message: str, intent: str) -> tuple[str, ...]:
    nodes: list[str] = ["business_intent"]
    nodes.extend(suggest_revenue_diagnostic_nodes(message, intent))
    if (
        intent
        in {"new_topic", "mixed_question", "correction", "clarification_answer", "artifact_continue"}
        and "compare_periods" not in nodes
    ):
        nodes.append("compare_periods")
    if "answer_verify" not in nodes:
        nodes.append("answer_verify")
    return tuple(dict.fromkeys(nodes))


def _runtime_budget(message: str) -> dict[str, int | str]:
    deep = any(token in message for token in ("深挖", "再找原因", "为什么", "原因"))
    return {"mode": "deep_attribution" if deep else "normal", "soft_limit": 100 if deep else 50}


def _can_read_scope(role: str, permission_scope: str) -> bool:
    return _can_read_permission_scope(role, permission_scope)


def _topic_title(message: str) -> str:
    return message[:28] or "新业务问题"


def _intent_summary(intent: str, message: str) -> str:
    if intent == "off_topic":
        return "这不是当前 BI Agent 要执行的业务分析输入。"
    if intent == "capability_question":
        return "用户在询问系统能力或证据边界。"
    if intent == "unsupported_request":
        return "用户请求触达权限或安全边界。"
    if intent == "mixed_question":
        return "用户把多个业务动作放在同一轮输入里。"
    if intent == "memory_update":
        return "用户提出可沉淀的分析偏好。"
    return message


def _response_boundary(intent: str) -> str:
    return {
        "off_topic": "只回答 BI Agent 的业务分析范围。",
        "capability_question": "说明系统能力、数据边界和证据边界。",
        "unsupported_request": "拒绝越权或不安全请求，并给出聚合替代路径。",
        "memory_update": "生成可审计记忆提案，不直接写入长期记忆。",
    }.get(intent, "进入受控 BI workflow，所有 claim 需要证据和 verifier。")
