from __future__ import annotations

from typing import Any, Mapping, Optional
from uuid import uuid4

from bi_agent.conversation.models import (
    ContextItem,
    ContextManifest,
    ConversationRunRequest,
    ConversationTurnResult,
    InteractionResponse,
    MemoryProposal,
    TopicChoiceInteractionResponse,
    TopicChoiceOption,
    TopicState,
    TurnIntent,
)
from bi_agent.conversation.entry_authority import (
    build_conversation_entry_transition,
)
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.durable_call_journal import (
    DurableCallJournalError,
    DurableCallSpec,
    DurableProviderClient,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.llm_client import (
    LLMConfigurationError,
    LLMOutputError,
    LLMProviderError,
    LLMTimeoutError,
)


ALLOWED_INTENTS = frozenset(
    {
        "new_topic",
        "follow_up",
        "mixed_question",
        "correction",
        "clarification_answer",
        "challenge",
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
        "select_referenced_topic",
        "ask_topic_choice",
        "queued_new_topic",
        "rejected",
    }
)
INTERACTION_INTENTS = frozenset(
    {"capability_question", "off_topic", "unsupported_request", "memory_update"}
)


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
        self.call_journal = getattr(self.store, "attempt_journal", None)

    def handle_message(
        self,
        thread_id: str,
        user_message: str,
        *,
        active_run_status: str = "idle",
        current_snapshot: str | None = None,
        contract_version: str | None = None,
        run_id: str | None = None,
        analysis_context: Mapping[str, Any] | None = None,
        topic_selection_binding: Mapping[str, Any] | None = None,
        pending_topic_choice: Mapping[str, Any] | None = None,
    ) -> ConversationTurnResult:
        run_attempt_id = run_id or f"conversation-run-{uuid4().hex[:12]}"
        lock = getattr(self.store, "conversation_entry_lock", None)
        if not callable(lock):
            raise ConversationOrchestrationError("conversation_entry_lock_required")
        with lock(run_attempt_id):
            return self._handle_message_locked(
                thread_id=thread_id,
                user_message=user_message,
                active_run_status=active_run_status,
                current_snapshot=current_snapshot,
                contract_version=contract_version,
                run_attempt_id=run_attempt_id,
                analysis_context=analysis_context,
                topic_selection_binding=topic_selection_binding,
                pending_topic_choice=pending_topic_choice,
            )

    def _handle_message_locked(
        self,
        *,
        thread_id: str,
        user_message: str,
        active_run_status: str,
        current_snapshot: str | None,
        contract_version: str | None,
        run_attempt_id: str,
        analysis_context: Mapping[str, Any] | None,
        topic_selection_binding: Mapping[str, Any] | None,
        pending_topic_choice: Mapping[str, Any] | None,
    ) -> ConversationTurnResult:
        thread = self.store.get_thread(thread_id)
        owner_id = str(thread.owner_id or "").strip()
        if not owner_id:
            raise ConversationOrchestrationError("thread_owner_missing")
        if not isinstance(user_message, str) or not user_message.strip():
            raise ConversationOrchestrationError("conversation_message_invalid")
        if active_run_status not in {"idle", "running"}:
            raise ConversationOrchestrationError(
                "conversation_active_run_status_invalid"
            )
        candidate_topics = self.store.topics_for_thread(thread_id)[-5:]
        current_topic = self.store.current_topic(thread_id)
        validated_pending_choice = (
            _validated_pending_topic_choice(
                pending_topic_choice,
                candidate_topics=candidate_topics,
            )
            if pending_topic_choice is not None
            else None
        )
        provider_input = {
            "user_message": user_message,
            "thread_state": {
                "thread_id": thread_id,
                "current_topic_id": thread.current_topic_id,
                "active_run_status": active_run_status,
            },
            "candidate_topics": [topic.to_dict() for topic in candidate_topics],
            "recent_turns": list(getattr(thread, "turns", [])[-5:]),
            "allowed_intents": sorted(ALLOWED_INTENTS),
            "allowed_topic_relations": sorted(ALLOWED_TOPIC_RELATIONS),
            "pending_topic_choice": validated_pending_choice,
        }
        orchestration_snapshot = {
            "provider_input": provider_input,
            "manifest_context": {
                "current_snapshot": current_snapshot,
                "contract_version": contract_version,
            },
            "current_topic": (
                current_topic.to_dict() if current_topic is not None else None
            ),
            "memory_items": [
                memory.to_dict() for memory in self.store.long_term_memory(owner_id)
            ],
        }
        command_payload = canonical_value(
            {
                "schema_version": "conversation-entry-command-envelope.v1",
                "thread_id": thread_id,
                "user_message": user_message,
                "analysis_context": dict(analysis_context or {}),
                "topic_selection_binding": topic_selection_binding,
                "pending_topic_choice": pending_topic_choice,
            }
        )
        claim_command = getattr(self.store, "claim_conversation_entry_command", None)
        if not callable(claim_command):
            raise ConversationOrchestrationError(
                "conversation_entry_command_store_required"
            )
        command_state = claim_command(
            run_attempt_id=run_attempt_id,
            thread_id=thread_id,
            command_payload=command_payload,
            orchestration_input=orchestration_snapshot,
        )
        snapshot = command_state.get("orchestration_input")
        if not isinstance(snapshot, Mapping):
            raise ConversationOrchestrationError("conversation_entry_command_invalid")
        stored_provider_input = snapshot.get("provider_input")
        if not isinstance(stored_provider_input, Mapping):
            raise ConversationOrchestrationError("conversation_entry_command_invalid")
        stored_candidate_topics = tuple(
            _topic_state_from_mapping(item)
            for item in stored_provider_input.get("candidate_topics") or ()
        )
        (
            orchestration,
            call_spec,
            accepted_attempt_ref,
            accepted_output_payload,
        ) = self._orchestrate_turn(
            run_attempt_id=run_attempt_id,
            command_state=command_state,
            provider_input=stored_provider_input,
            candidate_topics=stored_candidate_topics,
            active_run_status=str(
                stored_provider_input.get("thread_state", {}).get("active_run_status")
                or ""
            ),
            topic_selection_binding=topic_selection_binding,
        )
        command_digest = str(command_state.get("command_digest") or "")
        manifest_context = snapshot.get("manifest_context")
        if not isinstance(manifest_context, Mapping):
            raise ConversationOrchestrationError("conversation_entry_command_invalid")
        turn_id = _stable_conversation_ref("turn", run_attempt_id, command_digest)
        intent_name = orchestration["intent"]
        topic_relation = orchestration["topic_relation"]
        topic, topic_is_new, set_current_topic = self._resolve_topic(
            thread_id,
            topic_relation,
            user_message,
            run_attempt_id=run_attempt_id,
            command_digest=command_digest,
            current_topic=_optional_topic_state(snapshot.get("current_topic")),
            candidate_topics=stored_candidate_topics,
            selected_topic_id=orchestration["selected_topic_id"],
        )
        turn_intent = TurnIntent(
            intent=intent_name,
            confidence=orchestration["confidence"],
            topic_relation=topic_relation,
            decision_source=orchestration["decision_source"],
            business_summary=orchestration["business_summary"],
        )
        prior_topic_material_context: dict[str, Any] = {}
        manifest = self._context_manifest(
            manifest_id=_stable_conversation_ref(
                "context", run_attempt_id, command_digest
            ),
            run_attempt_id=run_attempt_id,
            thread_id=thread_id,
            turn_id=turn_id,
            topic=topic,
            message=user_message,
            current_snapshot=manifest_context.get("current_snapshot"),
            contract_version=manifest_context.get("contract_version"),
            created_at=str(command_state.get("claimed_at") or ""),
            memory_items=tuple(snapshot.get("memory_items") or ()),
            accepted_assumptions=(),
            pending_topic_choice=stored_provider_input.get("pending_topic_choice"),
        )
        memory_proposals = self._memory_proposals(
            thread_id,
            turn_id,
            user_message,
            intent_name,
            owner_id,
            proposal_id=_stable_conversation_ref(
                "memory-proposal", run_attempt_id, command_digest
            ),
        )
        run_request = None
        interaction_response = None
        if topic_relation == "ask_topic_choice":
            interaction_response = TopicChoiceInteractionResponse(
                schema_version="typed-topic-choice.v1",
                intent=intent_name,
                response_text=orchestration["display_summary"],
                options=tuple(
                    TopicChoiceOption(**option)
                    for option in orchestration["topic_options"]
                ),
                recommended_topic_id=orchestration["recommended_topic_id"],
                allow_free_text=True,
            )
        elif _should_run(intent_name):
            run_request = ConversationRunRequest(
                thread_id=thread_id,
                turn_id=turn_id,
                topic_id=topic.topic_id if topic else None,
                user_message=user_message,
                context_manifest=manifest.to_dict(),
                analysis_context=dict(analysis_context or {}),
                prior_topic_material_context=prior_topic_material_context,
            )
        else:
            interaction_response = InteractionResponse(
                schema_version="typed-interaction.v1",
                intent=intent_name,
                response_text=orchestration["display_summary"],
            )
        audit_events = (
            {
                "event": "turn_intent_bound",
                "turn_id": turn_id,
                "intent": intent_name,
                "topic_relation": topic_relation,
                "source": turn_intent.decision_source,
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
        if topic_selection_binding is not None:
            audit_events = audit_events + (
                {
                    "event": "topic_choice_applied",
                    "turn_id": turn_id,
                    "source_run_id": orchestration["source_run_id"],
                    "topic_id": orchestration["selected_topic_id"],
                },
            )
        if pending_topic_choice is not None:
            audit_events = audit_events + (
                {
                    "event": "topic_choice_free_text_bound",
                    "turn_id": turn_id,
                    "source_run_id": pending_topic_choice["source_run_id"],
                },
            )
        result = ConversationTurnResult(
            thread_id=thread_id,
            turn_id=turn_id,
            topic_id=topic.topic_id if topic else None,
            turn_intent=turn_intent,
            topic_relation=topic_relation,
            context_manifest=manifest,
            entry_command=canonical_value(
                {
                    **dict(command_state),
                    "accepted_attempt_ref": accepted_attempt_ref,
                }
            ),
            memory_proposals=memory_proposals,
            audit_events=audit_events,
            run_request=run_request,
            interaction_response=interaction_response,
        )
        transition, _, _ = build_conversation_entry_transition(
            run_attempt_id=run_attempt_id,
            command_state=command_state,
            call_spec=call_spec,
            accepted_attempt_ref=accepted_attempt_ref,
            accepted_output_payload=accepted_output_payload,
            orchestration=orchestration,
            topic=topic,
            topic_is_new=topic_is_new,
            set_current_topic=set_current_topic,
            turn=result.to_dict(),
            manifest=manifest,
        )
        accept_entry = getattr(self.store, "accept_conversation_entry", None)
        if not callable(accept_entry):
            raise ConversationOrchestrationError(
                "conversation_entry_acceptance_store_required"
            )
        accept_entry(
            run_attempt_id=run_attempt_id,
            command_state=command_state,
            call_spec=call_spec,
            accepted_attempt_ref=accepted_attempt_ref,
            orchestration=orchestration,
            transition=transition,
            topic=topic,
            topic_is_new=topic_is_new,
            set_current_topic=set_current_topic,
            turn=result.to_dict(),
            manifest=manifest,
        )
        for proposal in memory_proposals:
            self.store.add_memory_proposal(proposal)
        return result

    def _orchestrate_turn(
        self,
        *,
        run_attempt_id: str,
        command_state: Mapping[str, Any],
        provider_input: Mapping[str, Any],
        candidate_topics: tuple[TopicState, ...],
        active_run_status: str,
        topic_selection_binding: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], DurableCallSpec, str, dict[str, Any]]:
        if self.call_journal is None:
            raise ConversationOrchestrationError(
                "conversation_orchestrator_journal_required"
            )
        if topic_selection_binding is None:
            from bi_agent.runtime.llm_prompts import build_prompt

            prompt = build_prompt(
                "conversation_orchestrator",
                provider_input,
            )
            if not self.llm_client:
                raise ConversationOrchestrationError(
                    "conversation_orchestrator_provider_required"
                )

            def validate_provider_output(output: Mapping[str, Any]) -> None:
                try:
                    _validated_orchestration(
                        output,
                        active_run_status=active_run_status,
                        candidate_topics=candidate_topics,
                    )
                except ConversationOrchestrationError as exc:
                    raise LLMOutputError(str(exc)) from exc

            durable_client = DurableProviderClient(
                self.llm_client,
                journal=self.call_journal,
                run_attempt_id=run_attempt_id,
                intent_revision_id=None,
                plan_revision_id=None,
                call_kind="conversation_provider",
                task_id=None,
                stage_name="conversation_entry",
            )
            invoke_kwargs: dict[str, Any] = {
                "task": prompt.task,
                "prompt_version": prompt.prompt_version,
                "messages": prompt.messages,
                "required_keys": prompt.required_keys,
                "output_validator": validate_provider_output,
            }
            try:
                result = durable_client.invoke_json(**invoke_kwargs)
            except DurableCallJournalError as exc:
                raise ConversationOrchestrationError(
                    "conversation_orchestrator_failure_journal_failed"
                ) from exc
            except LLMOutputError as exc:
                raise ConversationOrchestrationError(str(exc)) from exc
            except (
                LLMConfigurationError,
                LLMProviderError,
                LLMTimeoutError,
            ) as exc:
                raise ConversationOrchestrationError(
                    "conversation_orchestrator_provider_failed"
                ) from exc
            validated = _validated_orchestration(
                dict(result.output),
                active_run_status=active_run_status,
                candidate_topics=candidate_topics,
            )
            if (
                len(durable_client.accepted_call_specs) != 1
                or len(durable_client.accepted_attempt_refs) != 1
            ):
                raise ConversationOrchestrationError(
                    "conversation_orchestrator_acceptance_invalid"
                )
            return (
                validated,
                durable_client.accepted_call_specs[0],
                durable_client.accepted_attempt_refs[0],
                canonical_value({"output": result.output, "audit": result.audit}),
            )

        call_input = canonical_value(
            {
                "schema_version": "topic-selection-control-input.v1",
                "topic_selection_binding": topic_selection_binding,
                "command_digest": command_state.get("command_digest"),
            }
        )
        call_spec = DurableCallSpec.create(
            run_attempt_id=run_attempt_id,
            intent_revision_id=None,
            plan_revision_id=None,
            task_id=None,
            stage_name="conversation_entry",
            call_kind="topic_selection",
            operation_name="apply_topic_selection",
            input_ref="topic-selection-input:sha256:" + canonical_digest(call_input),
            input_payload=call_input,
        )
        claim = self.call_journal.claim(call_spec)
        if claim.replayed:
            orchestration = _journaled_topic_selection_output(
                claim.output_payload,
                active_run_status=active_run_status,
                candidate_topics=candidate_topics,
            )
            return (
                orchestration,
                call_spec,
                claim.attempt.attempt_ref,
                canonical_value(claim.output_payload),
            )
        try:
            validated = _validated_topic_selection_binding(
                topic_selection_binding,
                candidate_topics=candidate_topics,
            )
        except Exception as exc:
            try:
                self.call_journal.fail(
                    claim.attempt,
                    failure_code=type(exc).__name__,
                    failure_payload=None,
                )
            except DurableCallJournalError as journal_exc:
                raise ConversationOrchestrationError(
                    "conversation_orchestrator_failure_journal_failed"
                ) from journal_exc
            if isinstance(
                exc,
                (
                    LLMConfigurationError,
                    LLMOutputError,
                    LLMProviderError,
                    LLMTimeoutError,
                ),
            ):
                raise ConversationOrchestrationError(
                    "conversation_orchestrator_provider_failed"
                ) from exc
            raise
        completion = self.call_journal.succeed(
            claim.attempt,
            {
                "schema_version": "topic-selection-control-output.v1",
                "orchestration": validated,
            },
        )
        if completion.disposition != "accepted" or completion.acceptance is None:
            raise DurableCallJournalError("call_success_orphaned")
        accepted = _journaled_topic_selection_output(
            completion.output_payload,
            active_run_status=active_run_status,
            candidate_topics=candidate_topics,
        )
        return (
            accepted,
            call_spec,
            completion.acceptance.accepted_attempt_ref,
            canonical_value(completion.output_payload),
        )

    def _resolve_topic(
        self,
        thread_id: str,
        relation: str,
        message: str,
        *,
        run_attempt_id: str,
        command_digest: str,
        current_topic: Optional[TopicState],
        candidate_topics: tuple[TopicState, ...],
        selected_topic_id: str | None,
    ) -> tuple[Optional[TopicState], bool, bool]:
        if relation in {"rejected", "ask_topic_choice"}:
            return current_topic, False, False
        if relation == "select_referenced_topic":
            topic = next(
                (
                    item
                    for item in candidate_topics
                    if item.topic_id == selected_topic_id
                ),
                None,
            )
            if topic is None or topic.thread_id != thread_id:
                raise ConversationOrchestrationError(
                    "conversation_orchestrator_topic_selection_invalid"
                )
            return topic, False, True
        if relation in {"new_topic", "queued_new_topic"}:
            topic = TopicState(
                topic_id=_stable_conversation_ref(
                    "topic", run_attempt_id, command_digest
                ),
                thread_id=thread_id,
                title=_topic_title(message),
                summary=message,
            )
            return topic, True, True
        return current_topic, False, False

    def _context_manifest(
        self,
        *,
        manifest_id: str,
        run_attempt_id: str,
        thread_id: str,
        turn_id: str,
        topic: Optional[TopicState],
        message: str,
        current_snapshot: str | None,
        contract_version: str | None,
        created_at: str,
        memory_items: tuple[Mapping[str, Any], ...],
        accepted_assumptions: tuple[Mapping[str, Any], ...] = (),
        pending_topic_choice: Mapping[str, Any] | None = None,
    ) -> ContextManifest:
        items: list[ContextItem] = []
        if topic:
            items.append(
                ContextItem(
                    source_type="topic",
                    source_ref=topic.topic_id,
                    summary=topic.summary,
                    can_support_claims=False,
                    reason="topic_context_only",
                    source_version=contract_version or "",
                    claim_use="context_only",
                )
            )
        if pending_topic_choice is not None:
            items.append(
                ContextItem(
                    source_type="topic_choice",
                    source_ref=str(pending_topic_choice["source_run_id"]),
                    summary=str(pending_topic_choice["source_user_message"]),
                    can_support_claims=False,
                    reason="conversation_control_only",
                    source_version="topic-choice-context.v1",
                    claim_use="context_only",
                )
            )
        for memory in memory_items:
            items.append(
                ContextItem(
                    source_type="memory",
                    source_ref=str(memory.get("source_ref") or ""),
                    summary=str(memory.get("text") or ""),
                    can_support_claims=False,
                    reason="preference_only",
                    source_version=str(memory.get("ttl") or ""),
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
                    source_version=contract_version or "",
                    claim_use="context_only",
                )
            )
        has_claim_support = any(item.can_support_claims for item in items)
        return ContextManifest(
            manifest_id=manifest_id,
            thread_id=thread_id,
            turn_id=turn_id,
            topic_id=topic.topic_id if topic else None,
            run_id=run_attempt_id,
            items=tuple(items),
            claim_use_policy={
                "requires_evidence_ref": True,
                "can_support_bi_claim": has_claim_support,
            },
            snapshot_version=current_snapshot,
            accepted_assumptions=[dict(item) for item in accepted_assumptions],
            contract_versions=(
                {"runtime": contract_version} if contract_version else {}
            ),
            schema_fingerprint=(
                f"{contract_version}:{current_snapshot}"
                if contract_version and current_snapshot
                else ""
            ),
            created_at=created_at,
            can_support_claims=has_claim_support,
        )

    def _memory_proposals(
        self,
        thread_id: str,
        turn_id: str,
        message: str,
        intent: str,
        owner_id: str,
        proposal_id: str,
    ) -> tuple[MemoryProposal, ...]:
        if intent != "memory_update":
            return ()
        return (
            MemoryProposal(
                proposal_id=proposal_id,
                thread_id=thread_id,
                text=message.strip(),
                source_ref=turn_id,
                owner_id=owner_id,
            ),
        )


def _journaled_topic_selection_output(
    payload: Mapping[str, Any] | None,
    *,
    active_run_status: str,
    candidate_topics: tuple[TopicState, ...],
) -> dict[str, Any]:
    if (
        not isinstance(payload, Mapping)
        or set(payload)
        != {
            "schema_version",
            "orchestration",
        }
        or payload.get("schema_version") != "topic-selection-control-output.v1"
    ):
        raise ConversationOrchestrationError("topic_selection_journal_output_invalid")
    raw = payload.get("orchestration")
    if not isinstance(raw, Mapping):
        raise ConversationOrchestrationError("topic_selection_journal_output_invalid")
    validated = _validated_orchestration(
        dict(raw),
        active_run_status=active_run_status,
        candidate_topics=candidate_topics,
    )
    source_run_id = raw.get("source_run_id")
    if (
        raw.get("decision_source") != "persisted_topic_choice"
        or not isinstance(source_run_id, str)
        or not source_run_id.strip()
        or source_run_id != source_run_id.strip()
    ):
        raise ConversationOrchestrationError("topic_selection_journal_output_invalid")
    validated["decision_source"] = "persisted_topic_choice"
    validated["source_run_id"] = source_run_id
    return validated


def _topic_state_from_mapping(value: Any) -> TopicState:
    if not isinstance(value, Mapping) or set(value) != {
        "topic_id",
        "thread_id",
        "title",
        "summary",
        "status",
        "assumptions",
        "open_questions",
    }:
        raise ConversationOrchestrationError(
            "conversation_entry_topic_snapshot_invalid"
        )
    return TopicState(
        topic_id=str(value["topic_id"]),
        thread_id=str(value["thread_id"]),
        title=str(value["title"]),
        summary=str(value["summary"]),
        status=str(value["status"]),
        assumptions=tuple(value["assumptions"]),
        open_questions=tuple(value["open_questions"]),
    )


def _optional_topic_state(value: Any) -> TopicState | None:
    return None if value is None else _topic_state_from_mapping(value)


def _stable_conversation_ref(
    prefix: str,
    run_attempt_id: str,
    command_digest: str,
) -> str:
    if not command_digest:
        raise ConversationOrchestrationError("conversation_entry_command_invalid")
    return f"{prefix}-{canonical_digest({'run_attempt_id': run_attempt_id, 'command_digest': command_digest, 'kind': prefix})[:24]}"


def _validated_orchestration(
    output: Any,
    *,
    active_run_status: str,
    candidate_topics: tuple[TopicState, ...],
) -> dict[str, Any]:
    if not isinstance(output, dict):
        raise ConversationOrchestrationError("conversation_orchestrator_output_invalid")

    intent = str(output.get("intent") or "").strip()
    topic_relation = str(output.get("topic_relation") or "").strip()
    if intent not in ALLOWED_INTENTS or topic_relation not in ALLOWED_TOPIC_RELATIONS:
        raise ConversationOrchestrationError("conversation_orchestrator_output_invalid")
    if intent in INTERACTION_INTENTS:
        if topic_relation not in {"inherit_current", "rejected"}:
            raise ConversationOrchestrationError(
                "conversation_orchestrator_state_invalid"
            )
    elif topic_relation == "rejected":
        raise ConversationOrchestrationError("conversation_orchestrator_state_invalid")
    candidate_by_id = {topic.topic_id: topic for topic in candidate_topics}
    topic_count = len(candidate_by_id)
    selected_topic_id = output.get("selected_topic_id")
    raw_topic_options = output.get("topic_options")
    recommended_topic_id = output.get("recommended_topic_id")
    if topic_relation == "select_referenced_topic":
        if (
            not isinstance(selected_topic_id, str)
            or selected_topic_id not in candidate_by_id
        ):
            raise ConversationOrchestrationError(
                "conversation_orchestrator_topic_selection_invalid"
            )
        if raw_topic_options not in (None, []) or recommended_topic_id is not None:
            raise ConversationOrchestrationError(
                "conversation_orchestrator_topic_selection_invalid"
            )
        topic_options = []
    elif topic_relation == "ask_topic_choice":
        if selected_topic_id is not None:
            raise ConversationOrchestrationError(
                "conversation_orchestrator_topic_choice_invalid"
            )
        topic_options = _validated_topic_options(
            raw_topic_options,
            candidate_by_id=candidate_by_id,
        )
        if not isinstance(recommended_topic_id, str) or recommended_topic_id not in {
            option["topic_id"] for option in topic_options
        }:
            raise ConversationOrchestrationError(
                "conversation_orchestrator_topic_choice_invalid"
            )
    else:
        if (
            selected_topic_id is not None
            or raw_topic_options not in (None, [])
            or recommended_topic_id is not None
        ):
            raise ConversationOrchestrationError(
                "conversation_orchestrator_topic_binding_unexpected"
            )
        topic_options = []
    if (
        topic_count == 0
        and _should_run(intent)
        and topic_relation not in {"new_topic", "queued_new_topic"}
    ):
        raise ConversationOrchestrationError("conversation_orchestrator_state_invalid")
    if active_run_status == "running" and topic_relation == "new_topic":
        raise ConversationOrchestrationError("conversation_orchestrator_state_invalid")

    business_summary = output.get("business_summary")
    display_summary = output.get("display_summary")
    if not isinstance(business_summary, str) or not business_summary.strip():
        raise ConversationOrchestrationError(
            "conversation_orchestrator_business_summary_invalid"
        )
    if not isinstance(display_summary, str) or not display_summary.strip():
        raise ConversationOrchestrationError(
            "conversation_orchestrator_interaction_response_invalid"
        )

    return {
        "intent": intent,
        "topic_relation": topic_relation,
        "confidence": _validated_confidence(output.get("confidence")),
        "decision_source": "llm_conversation_orchestrator",
        "business_summary": business_summary.strip(),
        "display_summary": display_summary.strip(),
        "selected_topic_id": selected_topic_id,
        "topic_options": topic_options,
        "recommended_topic_id": recommended_topic_id,
    }


def _validated_topic_options(
    value: Any,
    *,
    candidate_by_id: Mapping[str, TopicState],
) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 2 <= len(value) <= 3:
        raise ConversationOrchestrationError(
            "conversation_orchestrator_topic_choice_invalid"
        )
    normalized: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "topic_id",
            "label",
            "description",
        }:
            raise ConversationOrchestrationError(
                "conversation_orchestrator_topic_choice_invalid"
            )
        topic_id = item.get("topic_id")
        label = item.get("label")
        description = item.get("description")
        if (
            not isinstance(topic_id, str)
            or topic_id not in candidate_by_id
            or not isinstance(label, str)
            or not label.strip()
            or label != label.strip()
            or not isinstance(description, str)
            or not description.strip()
            or description != description.strip()
        ):
            raise ConversationOrchestrationError(
                "conversation_orchestrator_topic_choice_invalid"
            )
        normalized.append(
            {
                "topic_id": topic_id,
                "label": label,
                "description": description,
            }
        )
    if len({item["topic_id"] for item in normalized}) != len(normalized):
        raise ConversationOrchestrationError(
            "conversation_orchestrator_topic_choice_invalid"
        )
    return normalized


def _validated_topic_selection_binding(
    value: Mapping[str, Any],
    *,
    candidate_topics: tuple[TopicState, ...],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "source_run_id",
        "intent",
        "confidence",
        "business_summary",
        "selected_topic_id",
    }:
        raise ConversationOrchestrationError("topic_selection_binding_invalid")
    if value.get("schema_version") != "persisted-topic-selection.v1":
        raise ConversationOrchestrationError("topic_selection_binding_invalid")
    source_run_id = value.get("source_run_id")
    intent = value.get("intent")
    business_summary = value.get("business_summary")
    selected_topic_id = value.get("selected_topic_id")
    candidate_ids = {topic.topic_id for topic in candidate_topics}
    if (
        not isinstance(source_run_id, str)
        or not source_run_id.strip()
        or source_run_id != source_run_id.strip()
        or intent not in ALLOWED_INTENTS
        or intent in INTERACTION_INTENTS
        or not isinstance(business_summary, str)
        or not business_summary.strip()
        or business_summary != business_summary.strip()
        or not isinstance(selected_topic_id, str)
        or selected_topic_id not in candidate_ids
    ):
        raise ConversationOrchestrationError("topic_selection_binding_invalid")
    return {
        "intent": intent,
        "topic_relation": "select_referenced_topic",
        "confidence": _validated_confidence(value.get("confidence")),
        "decision_source": "persisted_topic_choice",
        "business_summary": business_summary,
        "display_summary": business_summary,
        "selected_topic_id": selected_topic_id,
        "topic_options": [],
        "recommended_topic_id": None,
        "source_run_id": source_run_id,
    }


def _validated_pending_topic_choice(
    value: Mapping[str, Any],
    *,
    candidate_topics: tuple[TopicState, ...],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "source_run_id",
        "source_user_message",
        "options",
    }:
        raise ConversationOrchestrationError("pending_topic_choice_invalid")
    if value.get("schema_version") != "pending-topic-choice.v1":
        raise ConversationOrchestrationError("pending_topic_choice_invalid")
    source_run_id = value.get("source_run_id")
    source_user_message = value.get("source_user_message")
    if (
        not isinstance(source_run_id, str)
        or not source_run_id.strip()
        or source_run_id != source_run_id.strip()
        or not isinstance(source_user_message, str)
        or not source_user_message.strip()
        or source_user_message != source_user_message.strip()
    ):
        raise ConversationOrchestrationError("pending_topic_choice_invalid")
    options = _validated_topic_options(
        value.get("options"),
        candidate_by_id={topic.topic_id: topic for topic in candidate_topics},
    )
    return {
        "schema_version": "pending-topic-choice.v1",
        "source_run_id": source_run_id,
        "source_user_message": source_user_message,
        "options": options,
    }


def _validated_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConversationOrchestrationError(
            "conversation_orchestrator_confidence_invalid"
        )
    confidence = float(value)
    if confidence < 0.0 or confidence > 1.0:
        raise ConversationOrchestrationError(
            "conversation_orchestrator_confidence_invalid"
        )
    return confidence


def _should_run(intent: str) -> bool:
    return intent not in INTERACTION_INTENTS


def _topic_title(message: str) -> str:
    return message[:28]
