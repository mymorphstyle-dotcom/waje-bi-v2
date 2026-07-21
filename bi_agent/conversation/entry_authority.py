from __future__ import annotations

from typing import Any, Mapping

from bi_agent.conversation.models import ContextItem, ContextManifest, TopicState
from bi_agent.runtime.durable_call_journal import DurableCallSpec
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)
from bi_agent.runtime.single_authority import DurableTransition


CONVERSATION_ENTRY_STAGE = "conversation_entry"
_PROVIDER_ORCHESTRATION_FIELDS = frozenset(
    {
        "intent",
        "topic_relation",
        "business_summary",
        "confidence",
        "display_summary",
        "selected_topic_id",
        "topic_options",
        "recommended_topic_id",
    }
)
_INTERACTION_INTENTS = frozenset(
    {"capability_question", "off_topic", "unsupported_request", "memory_update"}
)
_TURN_FIELDS = frozenset(
    {
        "thread_id",
        "turn_id",
        "topic_id",
        "turn_intent",
        "topic_relation",
        "context_manifest",
        "entry_command",
        "memory_proposals",
        "audit_events",
        "run_request",
        "interaction_response",
    }
)


def build_conversation_entry_transition(
    *,
    run_attempt_id: str,
    command_state: Mapping[str, Any],
    call_spec: DurableCallSpec,
    accepted_attempt_ref: str,
    accepted_output_payload: Mapping[str, Any],
    orchestration: Mapping[str, Any],
    topic: TopicState | None,
    topic_is_new: bool,
    set_current_topic: bool,
    turn: Mapping[str, Any],
    manifest: ContextManifest,
) -> tuple[DurableTransition, dict[str, Any], dict[str, Any]]:
    if type(call_spec) is not DurableCallSpec:
        raise EvidenceIntegrityError("conversation_entry_call_spec_invalid")
    spec = DurableCallSpec.from_dict(call_spec.to_dict())
    state = canonical_value(command_state)
    accepted_output = canonical_value(accepted_output_payload)
    derived_orchestration = canonical_value(orchestration)
    turn_payload = canonical_value(turn)
    manifest_payload = canonical_value(manifest.to_dict())
    command_payload = state.get("command_payload") if isinstance(state, dict) else None
    expected_call_kind = (
        "topic_selection"
        if isinstance(command_payload, Mapping)
        and command_payload.get("topic_selection_binding") is not None
        else "conversation_provider"
    )
    if (
        not isinstance(state, dict)
        or not isinstance(accepted_output, dict)
        or not isinstance(derived_orchestration, dict)
        or not isinstance(turn_payload, dict)
        or not isinstance(manifest_payload, dict)
        or spec.run_attempt_id != run_attempt_id
        or spec.stage_name != CONVERSATION_ENTRY_STAGE
        or spec.call_kind != expected_call_kind
        or not isinstance(accepted_attempt_ref, str)
        or not accepted_attempt_ref.strip()
        or state.get("run_attempt_id") != run_attempt_id
    ):
        raise EvidenceIntegrityError("conversation_entry_authority_input_invalid")
    _validate_orchestration_projection(
        call_kind=spec.call_kind,
        accepted_output=accepted_output,
        orchestration=derived_orchestration,
    )
    _validate_turn_projection(
        run_attempt_id=run_attempt_id,
        command_state=state,
        accepted_attempt_ref=accepted_attempt_ref,
        orchestration=derived_orchestration,
        topic=topic,
        turn=turn_payload,
        manifest=manifest_payload,
    )
    route = _route_projection(turn_payload)
    input_payload = canonical_value(
        {
            "schema_version": "conversation-entry-transition-input.v1",
            "command_digest": state.get("command_digest"),
            "call_spec_ref": spec.spec_ref,
            "call_spec_digest": spec.content_digest,
            "call_input_digest": spec.input_digest,
            "call_idempotency_key": spec.idempotency_key,
            "accepted_attempt_ref": accepted_attempt_ref,
            "accepted_output_digest": canonical_digest(accepted_output),
            "orchestration": derived_orchestration,
        }
    )
    output_payload = canonical_value(
        {
            "schema_version": "conversation-entry-transition-output.v1",
            "route": route,
            "topic": topic.to_dict() if topic is not None else None,
            "topic_is_new": topic_is_new,
            "set_current_topic": set_current_topic,
            "turn": turn_payload,
            "manifest": manifest_payload,
        }
    )
    claimed_at = state.get("claimed_at")
    if not isinstance(claimed_at, str) or not claimed_at.strip():
        raise EvidenceIntegrityError("conversation_entry_claimed_at_invalid")
    transition = DurableTransition.create(
        node_name=CONVERSATION_ENTRY_STAGE,
        parent_transition_id=None,
        run_attempt_id=run_attempt_id,
        intent_revision_id="",
        decision_ledger_position=0,
        input_digest=canonical_digest(input_payload),
        output_digest=canonical_digest(output_payload),
        execution_attempt=1,
        provider_ref="waje-conversation-entry-authority",
        model_ref="conversation-entry-transition.v1",
        status="succeeded",
        acceptance_state="accepted",
        next_transition=_next_transition(route),
        started_at=claimed_at,
        finished_at=claimed_at,
    )
    return transition, input_payload, output_payload


def _validate_orchestration_projection(
    *,
    call_kind: str,
    accepted_output: Mapping[str, Any],
    orchestration: Mapping[str, Any],
) -> None:
    if call_kind == "conversation_provider" and set(accepted_output) == {
        "output",
        "audit",
    }:
        raw = accepted_output.get("output")
        audit = accepted_output.get("audit")
        if (
            not isinstance(raw, Mapping)
            or set(raw) != _PROVIDER_ORCHESTRATION_FIELDS
            or not isinstance(audit, Mapping)
        ):
            raise EvidenceIntegrityError("conversation_entry_provider_output_invalid")
        expected = {
            "intent": str(raw.get("intent") or "").strip(),
            "topic_relation": str(raw.get("topic_relation") or "").strip(),
            "confidence": _normalized_confidence(raw.get("confidence")),
            "decision_source": "llm_conversation_orchestrator",
            "business_summary": _stripped_text(
                raw.get("business_summary"),
                "conversation_entry_provider_output_invalid",
            ),
            "display_summary": _stripped_text(
                raw.get("display_summary"),
                "conversation_entry_provider_output_invalid",
            ),
            "selected_topic_id": raw.get("selected_topic_id"),
            "topic_options": canonical_value(raw.get("topic_options") or []),
            "recommended_topic_id": raw.get("recommended_topic_id"),
        }
    elif call_kind == "topic_selection" and set(accepted_output) == {
        "schema_version",
        "orchestration",
    }:
        raw = accepted_output.get("orchestration")
        if (
            accepted_output.get("schema_version") != "topic-selection-control-output.v1"
            or not isinstance(raw, Mapping)
            or raw.get("decision_source") != "persisted_topic_choice"
            or not isinstance(raw.get("source_run_id"), str)
            or not raw["source_run_id"].strip()
        ):
            raise EvidenceIntegrityError("conversation_entry_control_output_invalid")
        expected = canonical_value(raw)
    else:
        raise EvidenceIntegrityError("conversation_entry_call_output_invalid")
    if canonical_value(expected) != canonical_value(orchestration):
        raise EvidenceIntegrityError("conversation_entry_call_projection_mismatch")


def _validate_turn_projection(
    *,
    run_attempt_id: str,
    command_state: Mapping[str, Any],
    accepted_attempt_ref: str,
    orchestration: Mapping[str, Any],
    topic: TopicState | None,
    turn: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> None:
    thread_id = str(command_state.get("thread_id") or "")
    command_payload = command_state.get("command_payload")
    orchestration_input = command_state.get("orchestration_input")
    if not isinstance(command_payload, Mapping) or not isinstance(
        orchestration_input, Mapping
    ):
        raise EvidenceIntegrityError("conversation_entry_turn_projection_invalid")
    expected_topic = _expected_topic_projection(
        run_attempt_id=run_attempt_id,
        command_state=command_state,
        orchestration=orchestration,
    )
    topic_id = topic.topic_id if topic is not None else None
    turn_intent = turn.get("turn_intent")
    entry_command = turn.get("entry_command")
    if (
        not thread_id
        or set(turn) != _TURN_FIELDS
        or not isinstance(turn_intent, Mapping)
        or not isinstance(entry_command, Mapping)
        or turn.get("thread_id") != thread_id
        or turn.get("turn_id")
        != _stable_ref(
            "turn",
            run_attempt_id,
            str(command_state.get("command_digest") or ""),
        )
        or turn.get("topic_id") != topic_id
        or canonical_value(topic.to_dict() if topic is not None else None)
        != canonical_value(expected_topic)
        or turn.get("topic_relation") != orchestration.get("topic_relation")
        or canonical_value(turn.get("context_manifest")) != canonical_value(manifest)
        or manifest.get("run_id") != run_attempt_id
        or manifest.get("thread_id") != thread_id
        or manifest.get("turn_id") != turn.get("turn_id")
        or manifest.get("topic_id") != topic_id
        or canonical_value(entry_command)
        != canonical_value(
            {
                **dict(command_state),
                "accepted_attempt_ref": accepted_attempt_ref,
            }
        )
    ):
        raise EvidenceIntegrityError("conversation_entry_turn_projection_invalid")
    expected_intent = {
        "intent": orchestration.get("intent"),
        "confidence": orchestration.get("confidence"),
        "topic_relation": orchestration.get("topic_relation"),
        "decision_source": orchestration.get("decision_source"),
        "business_summary": orchestration.get("business_summary"),
    }
    if canonical_value(turn_intent) != canonical_value(expected_intent):
        raise EvidenceIntegrityError("conversation_entry_turn_projection_invalid")
    expected_manifest = _expected_manifest_projection(
        run_attempt_id=run_attempt_id,
        command_state=command_state,
        topic=topic,
        turn_id=str(turn.get("turn_id") or ""),
    )
    if canonical_value(manifest) != canonical_value(expected_manifest):
        raise EvidenceIntegrityError("conversation_entry_manifest_projection_invalid")
    _validate_route_projection(
        command_payload=command_payload,
        orchestration=orchestration,
        topic_id=topic_id,
        turn=turn,
        manifest=manifest,
    )


def _expected_topic_projection(
    *,
    run_attempt_id: str,
    command_state: Mapping[str, Any],
    orchestration: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    thread_id = str(command_state.get("thread_id") or "")
    digest = str(command_state.get("command_digest") or "")
    command = command_state.get("command_payload")
    snapshot = command_state.get("orchestration_input")
    if (
        not thread_id
        or not digest
        or not isinstance(command, Mapping)
        or not isinstance(snapshot, Mapping)
    ):
        raise EvidenceIntegrityError("conversation_entry_topic_projection_invalid")
    message = command.get("user_message")
    if not isinstance(message, str) or not message.strip():
        raise EvidenceIntegrityError("conversation_entry_topic_projection_invalid")
    relation = orchestration.get("topic_relation")
    if relation in {"new_topic", "queued_new_topic"}:
        return TopicState(
            topic_id=_stable_ref("topic", run_attempt_id, digest),
            thread_id=thread_id,
            title=message[:28],
            summary=message,
        ).to_dict()
    if relation == "select_referenced_topic":
        provider_input = snapshot.get("provider_input")
        candidates = (
            provider_input.get("candidate_topics")
            if isinstance(provider_input, Mapping)
            else None
        )
        selected = orchestration.get("selected_topic_id")
        if not isinstance(candidates, list):
            raise EvidenceIntegrityError("conversation_entry_topic_projection_invalid")
        matches = [
            item
            for item in candidates
            if isinstance(item, Mapping) and item.get("topic_id") == selected
        ]
        if len(matches) != 1:
            raise EvidenceIntegrityError("conversation_entry_topic_projection_invalid")
        return canonical_value(matches[0])
    current_topic = snapshot.get("current_topic")
    if current_topic is not None and not isinstance(current_topic, Mapping):
        raise EvidenceIntegrityError("conversation_entry_topic_projection_invalid")
    return canonical_value(current_topic)


def _expected_manifest_projection(
    *,
    run_attempt_id: str,
    command_state: Mapping[str, Any],
    topic: TopicState | None,
    turn_id: str,
) -> dict[str, Any]:
    thread_id = str(command_state.get("thread_id") or "")
    digest = str(command_state.get("command_digest") or "")
    snapshot = command_state.get("orchestration_input")
    if not isinstance(snapshot, Mapping) or not turn_id:
        raise EvidenceIntegrityError("conversation_entry_manifest_projection_invalid")
    manifest_context = snapshot.get("manifest_context")
    provider_input = snapshot.get("provider_input")
    memory_items = snapshot.get("memory_items")
    if (
        not isinstance(manifest_context, Mapping)
        or not isinstance(provider_input, Mapping)
        or not isinstance(memory_items, list)
    ):
        raise EvidenceIntegrityError("conversation_entry_manifest_projection_invalid")
    current_snapshot = manifest_context.get("current_snapshot")
    contract_version = manifest_context.get("contract_version")
    items: list[ContextItem] = []
    if topic is not None:
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
    pending_choice = provider_input.get("pending_topic_choice")
    if pending_choice is not None:
        if not isinstance(pending_choice, Mapping):
            raise EvidenceIntegrityError(
                "conversation_entry_manifest_projection_invalid"
            )
        items.append(
            ContextItem(
                source_type="topic_choice",
                source_ref=str(pending_choice.get("source_run_id") or ""),
                summary=str(pending_choice.get("source_user_message") or ""),
                can_support_claims=False,
                reason="conversation_control_only",
                source_version="topic-choice-context.v1",
                claim_use="context_only",
            )
        )
    for memory in memory_items:
        if not isinstance(memory, Mapping):
            raise EvidenceIntegrityError(
                "conversation_entry_manifest_projection_invalid"
            )
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
    can_support_claims = any(item.can_support_claims for item in items)
    return ContextManifest(
        manifest_id=_stable_ref("context", run_attempt_id, digest),
        thread_id=thread_id,
        turn_id=turn_id,
        topic_id=topic.topic_id if topic is not None else None,
        run_id=run_attempt_id,
        items=tuple(items),
        claim_use_policy={
            "requires_evidence_ref": True,
            "can_support_bi_claim": can_support_claims,
        },
        snapshot_version=current_snapshot,
        accepted_assumptions=[],
        contract_versions=({"runtime": contract_version} if contract_version else {}),
        schema_fingerprint=(
            f"{contract_version}:{current_snapshot}"
            if contract_version and current_snapshot
            else ""
        ),
        created_at=str(command_state.get("claimed_at") or ""),
        can_support_claims=can_support_claims,
    ).to_dict()


def _validate_route_projection(
    *,
    command_payload: Mapping[str, Any],
    orchestration: Mapping[str, Any],
    topic_id: str | None,
    turn: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> None:
    intent = str(orchestration.get("intent") or "")
    relation = str(orchestration.get("topic_relation") or "")
    run_request = turn.get("run_request")
    interaction = turn.get("interaction_response")
    if relation == "ask_topic_choice":
        expected_run = None
        expected_interaction = {
            "schema_version": "typed-topic-choice.v1",
            "intent": intent,
            "response_text": orchestration.get("display_summary"),
            "options": orchestration.get("topic_options"),
            "recommended_topic_id": orchestration.get("recommended_topic_id"),
            "allow_free_text": True,
        }
    elif intent in _INTERACTION_INTENTS:
        expected_run = None
        expected_interaction = {
            "schema_version": "typed-interaction.v1",
            "intent": intent,
            "response_text": orchestration.get("display_summary"),
        }
    else:
        expected_run = {
            "thread_id": turn.get("thread_id"),
            "turn_id": turn.get("turn_id"),
            "topic_id": topic_id,
            "user_message": command_payload.get("user_message"),
            "context_manifest": manifest,
            "analysis_context": canonical_value(
                command_payload.get("analysis_context") or {}
            ),
            "prior_topic_material_context": {},
        }
        expected_interaction = None
    if canonical_value(run_request) != canonical_value(expected_run) or canonical_value(
        interaction
    ) != canonical_value(expected_interaction):
        raise EvidenceIntegrityError("conversation_entry_route_invalid")


def _stable_ref(prefix: str, run_attempt_id: str, command_digest: str) -> str:
    return (
        f"{prefix}-"
        + canonical_digest(
            {
                "run_attempt_id": run_attempt_id,
                "command_digest": command_digest,
                "kind": prefix,
            }
        )[:24]
    )


def _route_projection(turn: Mapping[str, Any]) -> dict[str, Any]:
    run_request = turn.get("run_request")
    interaction = turn.get("interaction_response")
    active_routes = sum((run_request is not None, interaction is not None))
    if active_routes != 1:
        raise EvidenceIntegrityError("conversation_entry_route_invalid")
    if run_request is not None:
        if not isinstance(run_request, Mapping):
            raise EvidenceIntegrityError("conversation_entry_route_invalid")
        return canonical_value({"route_kind": "analysis_run", "payload": run_request})
    if interaction is not None:
        if not isinstance(interaction, Mapping):
            raise EvidenceIntegrityError("conversation_entry_route_invalid")
        route_kind = (
            "topic_choice"
            if interaction.get("schema_version") == "typed-topic-choice.v1"
            else "interaction"
        )
        return canonical_value({"route_kind": route_kind, "payload": interaction})
    raise EvidenceIntegrityError("conversation_entry_route_invalid")


def _next_transition(route: Mapping[str, Any]) -> str:
    return {
        "analysis_run": "bind_intent",
        "topic_choice": "waiting_for_topic_choice",
        "interaction": "conversation_complete",
    }[str(route["route_kind"])]


def _stripped_text(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceIntegrityError(error)
    return value.strip()


def _normalized_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceIntegrityError("conversation_entry_provider_output_invalid")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise EvidenceIntegrityError("conversation_entry_provider_output_invalid")
    return result
