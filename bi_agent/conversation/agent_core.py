from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Optional
from uuid import uuid4

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.conversation.material_revision_continuation import (
    MaterialRevisionContinuation,
)
from bi_agent.conversation.models import (
    ClarificationOption,
    ClarificationState,
    TopicChoiceInteractionResponse,
)
from bi_agent.conversation.runtime import (
    ConversationOrchestrationError,
    ConversationRuntime,
)
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)
from bi_agent.runtime.factor_coverage import (
    FactorCoveragePlan,
    FactorCoverageResult,
    InvestigationBranch,
    InvestigationSynthesis,
)
from bi_agent.runtime.durable_call_journal import (
    DurableCallJournal,
    DurableCallJournalError,
    DurableProviderClient,
)
from bi_agent.runtime.authoritative_plan_result import (
    AUTHORITATIVE_PLAN_RESULT_SCHEMA_VERSION,
    parse_authoritative_plan_result,
)
from bi_agent.runtime.authoritative_execution_result import (
    AuthoritativeExecutionResult,
    SCHEMA_VERSION as AUTHORITATIVE_EXECUTION_RESULT_SCHEMA_VERSION,
)
from bi_agent.runtime.analysis_performance import build_analysis_performance_profile
from bi_agent.runtime.capability_scheduler import (
    capability_execution_transition_input,
)
from bi_agent.runtime.claim_coverage import (
    CLAIM_COVERAGE_CHECKPOINT_SCHEMA_VERSION,
)
from bi_agent.runtime.langgraph_workflow import (
    run_single_authority_workflow,
    workflow_request_fields,
)
from bi_agent.runtime.llm_prompts import build_prompt
from bi_agent.runtime.post_execution_workflow import (
    PostExecutionWorkflowResult,
    validate_typed_post_execution_workflow_result,
)
from bi_agent.runtime.publication_persistence import (
    DeliveryMessage,
    DeliveryTransportResult,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from bi_agent.runtime.single_authority import (
    DurableTransition,
    InteractionDirective,
)
from bi_agent.runtime.temporal_comparison import (
    normalize_temporal_decision_value,
)
from bi_agent.runtime.agent_runtime_admission import (
    PostgresAgentRuntimeAdmissionLease,
)


WorkflowRunner = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class GatewayDeliveryTransport:
    channel: str = "gateway"

    def __call__(self, message: DeliveryMessage) -> DeliveryTransportResult:
        if (
            type(message) is not DeliveryMessage
            or message.channel != self.channel
            or not message.destination_ref.startswith("conversation:")
        ):
            raise EvidenceIntegrityError("gateway_delivery_message_invalid")
        receipt_digest = canonical_digest(
            {
                "outbox_ref": message.outbox_ref,
                "destination_ref": message.destination_ref,
                "channel": message.channel,
                "idempotency_key": message.idempotency_key,
            }
        )
        return DeliveryTransportResult.published(
            f"gateway-publication:sha256:{receipt_digest}"
        )


def _emit_agent_core_startup_ack() -> None:
    raw_fd = os.getenv("WAJE_AGENT_CORE_STARTUP_ACK_FD", "").strip()
    if not raw_fd:
        return
    try:
        fd = int(raw_fd)
        os.write(fd, b"WAJE_AGENT_CORE_RUNNING\n")
        os.close(fd)
    except (OSError, ValueError) as exc:
        raise RuntimeError("agent_core_startup_ack_failed") from exc


def _record_analysis_performance_profile(
    *,
    store: Any,
    registry: RuntimeContractRegistry | None,
    run_id: str,
    thread_id: str,
    topic_id: str,
    checkpoint_events: Sequence[Mapping[str, Any]],
) -> None:
    if registry is None:
        return
    capability_substages = tuple(
        dict(item)
        for event in checkpoint_events
        if isinstance(event, Mapping)
        for item in (event.get("capability_substages") or ())
        if isinstance(item, Mapping)
    )
    try:
        profile = build_analysis_performance_profile(
            run_id=run_id,
            checkpoint_events=checkpoint_events,
            policy=registry.analysis_performance_policy,
            capability_substages=capability_substages,
        )
        store.add_audit_event(
            "analysis_performance_profile_recorded",
            thread_id=thread_id,
            topic_id=topic_id,
            run_id=run_id,
            ref=profile.profile_ref,
            payload=profile.to_dict(),
        )
    except Exception as exc:
        try:
            store.add_audit_event(
                "analysis_performance_profile_failed",
                thread_id=thread_id,
                topic_id=topic_id,
                run_id=run_id,
                payload={
                    "schema_version": "analysis-performance-profile-error.v1",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "breach_action": "record_and_continue",
                },
            )
        except Exception:
            # Performance telemetry is audit-only and may not alter business execution.
            return


class ConversationAgentCore:
    def __init__(
        self,
        store: Any,
        *,
        workflow_runner: Optional[WorkflowRunner] = None,
        conversation_llm_client: Any = None,
        runtime_registry: RuntimeContractRegistry | None = None,
        release_resolver: Any = None,
        analysis_runtime: Any = None,
        post_execution_locale: str | None = None,
        publication_channel: str | None = None,
        delivery_transport: Callable[[DeliveryMessage], DeliveryTransportResult]
        | None = None,
    ) -> None:
        self.store = store
        self.workflow_runner = workflow_runner or run_single_authority_workflow
        self.conversation_llm_client = conversation_llm_client
        self.runtime_registry = runtime_registry
        self.release_resolver = release_resolver
        self.analysis_runtime = analysis_runtime
        self.post_execution_locale = post_execution_locale
        self.publication_channel = publication_channel
        self.delivery_transport = delivery_transport

    def run_message(
        self,
        *,
        thread_id: str,
        run_id: str | None = None,
        user_message: str,
        user_id: str | None = None,
        artifact_root: str = "artifacts/phase-7",
        clarification: dict[str, Any] | None = None,
        run_dispatch: dict[str, Any] | None = None,
        analysis_context: Mapping[str, Any] | None = None,
        intent_revision_context: Mapping[str, Any] | None = None,
        topic_selection: Mapping[str, Any] | None = None,
        topic_choice_answer: Mapping[str, Any] | None = None,
        stop_after_phase: str | None = None,
    ) -> dict[str, Any]:
        if stop_after_phase not in {
            None,
            "phase02",
            "phase03",
            "phase04",
            "phase05",
        }:
            raise ValueError("stop_after_phase_invalid")
        analysis_context = _validated_analysis_context(analysis_context)
        intent_revision_context = _validated_intent_revision_context(
            intent_revision_context
        )
        run_id = run_id or f"run-{uuid4().hex[:12]}"
        thread = self.store.get_thread(thread_id)
        if user_id and str(thread.owner_id or "") != str(user_id):
            raise EvidenceIntegrityError("thread_owner_mismatch")
        set_actor_id = getattr(self.store, "set_actor_id", None)
        if callable(set_actor_id):
            set_actor_id(str(user_id or "system"))
        effective_user_message = user_message
        topic_selection_binding = None
        pending_topic_choice = None
        if topic_selection is not None and topic_choice_answer is not None:
            raise EvidenceIntegrityError("topic_choice_submission_conflict")
        if topic_selection is not None:
            effective_user_message, topic_selection_binding = (
                _validated_topic_selection_submission(
                    store=self.store,
                    thread_id=thread_id,
                    topic_selection=topic_selection,
                )
            )
        elif topic_choice_answer is not None:
            effective_user_message, pending_topic_choice = (
                _validated_topic_choice_answer_submission(
                    store=self.store,
                    thread_id=thread_id,
                    user_message=user_message,
                    topic_choice_answer=topic_choice_answer,
                )
            )
        clarification_submission = _is_single_authority_clarification_submission(
            clarification=clarification,
            run_id=run_id,
            user_message=user_message,
        )
        if clarification_submission:
            if not run_dispatch:
                raise EvidenceIntegrityError("clarification_dispatch_required")
            claim_dispatch = getattr(self.store, "claim_run_dispatch", None)
            if not callable(claim_dispatch):
                raise EvidenceIntegrityError("run_dispatch_claim_resolver_missing")
            claim_dispatch(
                dispatch_id=str(run_dispatch.get("dispatch_id") or ""),
                run_id=run_id,
                thread_id=thread_id,
                dispatch_owner_id=str(run_dispatch.get("dispatch_owner_id") or ""),
                lease_epoch=run_dispatch.get("lease_epoch"),
            )
            _emit_agent_core_startup_ack()
            decision_result = _record_single_authority_clarification_submission(
                store=self.store,
                llm_client=self.conversation_llm_client,
                thread_id=thread_id,
                run_id=run_id,
                user_message=user_message,
                clarification=clarification or {},
            )
            if decision_result.get("status") != "decision_recorded":
                if decision_result.get("status") == "material_revision_required":
                    return self._run_material_revision_successor(
                        thread_id=thread_id,
                        source_run_id=run_id,
                        artifact_root=artifact_root,
                        user_id=user_id,
                        decision_result=decision_result,
                        stop_after_phase=stop_after_phase,
                    )
                return decision_result
            return self._resume_authoritative_plan_after_decision(
                thread_id=thread_id,
                run_id=run_id,
                artifact_root=artifact_root,
                decision_result=decision_result,
                stop_after_phase=stop_after_phase,
            )
        if run_dispatch:
            claim_dispatch = getattr(self.store, "claim_run_dispatch", None)
            if not callable(claim_dispatch):
                raise EvidenceIntegrityError("run_dispatch_claim_resolver_missing")
            claim_dispatch(
                dispatch_id=str(run_dispatch.get("dispatch_id") or ""),
                run_id=run_id,
                thread_id=thread_id,
                dispatch_owner_id=str(run_dispatch.get("dispatch_owner_id") or ""),
                lease_epoch=run_dispatch.get("lease_epoch"),
            )
        else:
            self.store.upsert_run(run_id, thread_id=thread_id, status="running")
        try:
            active_run_status_resolver = getattr(
                self.store, "active_conversation_run_status", None
            )
            if not callable(active_run_status_resolver):
                raise EvidenceIntegrityError(
                    "active_conversation_run_status_resolver_missing"
                )
            active_run_status = active_run_status_resolver(
                thread_id,
                exclude_run_id=run_id,
            )
            _emit_agent_core_startup_ack()
            turn = ConversationRuntime(
                self.store,
                llm_client=self.conversation_llm_client,
            ).handle_message(
                thread_id,
                effective_user_message,
                run_id=run_id,
                active_run_status=active_run_status,
                analysis_context=analysis_context,
                topic_selection_binding=topic_selection_binding,
                pending_topic_choice=pending_topic_choice,
            )
        except Exception as exc:
            failure_reason = _conversation_entry_failure_reason(exc)
            try:
                self.store.upsert_run(
                    run_id,
                    thread_id=thread_id,
                    status="failed",
                    request={
                        "failure_reason": failure_reason,
                        "failure_type": "conversation_orchestration",
                    },
                )
            except Exception as persistence_exc:
                exc.add_note(
                    "conversation_failure_persistence_failed:"
                    + type(persistence_exc).__name__
                )
            raise
        context_manifest = turn.context_manifest.to_dict()
        self.store.record_context_manifest(context_manifest)

        if not turn.run_request:
            if (
                turn.interaction_response is None
                or turn.interaction_response.intent != turn.turn_intent.intent
            ):
                raise EvidenceIntegrityError("typed_interaction_response_required")
            interaction_result = turn.interaction_response.to_dict()
            interaction_request: dict[str, Any] = {
                "interaction_result": interaction_result,
                "conversation_entry": canonical_value(turn.entry_command),
            }
            if interaction_result.get("schema_version") == "typed-topic-choice.v1":
                source_user_message = (
                    pending_topic_choice["source_user_message"]
                    if pending_topic_choice is not None
                    else effective_user_message.strip()
                )
                interaction_request["interaction_context"] = {
                    "schema_version": "topic-choice-context.v1",
                    "source_user_message": source_user_message,
                    "intent": turn.turn_intent.intent,
                    "confidence": turn.turn_intent.confidence,
                    "business_summary": turn.turn_intent.business_summary,
                }
            self.store.upsert_run(
                run_id,
                thread_id=thread_id,
                turn_id=turn.turn_id,
                topic_id=turn.topic_id or "",
                status="interaction_completed",
                request=interaction_request,
            )
            return {
                "status": "interaction_completed",
                "run_id": run_id,
                "turn_id": turn.turn_id,
                "topic_id": turn.topic_id,
                "intent": turn.turn_intent.intent,
                "topic_relation": turn.topic_relation,
                "context_manifest": context_manifest,
                "interaction_result": interaction_result,
            }

        request = turn.run_request.to_dict()
        request["context_manifest"] = context_manifest
        request["conversation_entry"] = canonical_value(turn.entry_command)
        request.update(
            {
                "run_id": run_id,
                "run_attempt_id": run_id,
                "question": (
                    _topic_choice_analysis_question(
                        source_user_message=pending_topic_choice["source_user_message"],
                        answer=effective_user_message,
                    )
                    if pending_topic_choice is not None
                    else effective_user_message
                ),
                "turn_intent": turn.turn_intent.intent,
                "topic_relation": turn.topic_relation,
                "artifact_root": artifact_root,
                "analysis_context": dict(turn.run_request.analysis_context or {}),
            }
        )
        request["authority_store"] = self.store
        if topic_selection_binding is not None:
            request["topic_selection"] = canonical_value(topic_selection_binding)
        if self.conversation_llm_client is not None:
            request["llm_client"] = self.conversation_llm_client
        if stop_after_phase is not None:
            request["stop_after_phase"] = stop_after_phase
        if _requires_post_execution_runtime(stop_after_phase):
            request.update(
                self._post_execution_runtime_bindings(
                    owner_ref=str(thread.owner_id or ""),
                    thread_id=thread_id,
                )
            )
        if intent_revision_context:
            request.update(intent_revision_context)
        if self.runtime_registry is not None:
            request["runtime_registry"] = self.runtime_registry
        if self.release_resolver is not None:
            request["release_resolver"] = self.release_resolver
        if self.analysis_runtime is not None:
            request["analysis_runtime"] = self.analysis_runtime
        self.store.upsert_run(
            run_id,
            thread_id=thread_id,
            turn_id=turn.turn_id,
            topic_id=turn.topic_id or "",
            status="running_workflow",
            request=_persistable_request(request),
        )
        result = self.workflow_runner(_workflow_authority_request(request))
        workflow_llm_calls = tuple(
            dict(call) for call in result.llm_calls if isinstance(call, Mapping)
        )
        self.store.record_run_nodes(run_id, tuple(result.checkpoint_events))
        _record_analysis_performance_profile(
            store=self.store,
            registry=self.runtime_registry,
            run_id=run_id,
            thread_id=thread_id,
            topic_id=turn.topic_id or "",
            checkpoint_events=tuple(result.checkpoint_events),
        )
        if result.status == "waiting_for_clarification" and result.interaction_result:
            if result.interaction_result.get("schema_version") != (
                "single-authority-phase01.v1"
            ):
                raise EvidenceIntegrityError("single_authority_waiting_result_required")
            return _finalize_single_authority_waiting(
                store=self.store,
                interaction_result=result.interaction_result,
                run_id=run_id,
                thread_id=thread_id,
                turn_id=turn.turn_id,
                topic_id=turn.topic_id or "",
                request=request,
                context_manifest=context_manifest,
                turn_intent=turn.turn_intent.intent,
                topic_relation=turn.topic_relation,
                llm_calls=workflow_llm_calls,
            )
        if result.status == "planned":
            return _finalize_authoritative_plan(
                store=self.store,
                plan_result=result.plan_result,
                run_id=run_id,
                thread_id=thread_id,
                turn_id=turn.turn_id,
                topic_id=turn.topic_id or "",
                request=request,
                context_manifest=context_manifest,
                turn_intent=turn.turn_intent.intent,
                topic_relation=turn.topic_relation,
                llm_calls=workflow_llm_calls,
            )
        if result.status == "evidence_ready":
            return _finalize_capability_execution(
                store=self.store,
                plan_result=result.plan_result,
                execution_result=result.execution_result,
                factor_coverage_plan=result.factor_coverage_plan,
                factor_coverage_result=result.factor_coverage_result,
                investigation_branches=result.investigation_branches,
                investigation_synthesis=result.investigation_synthesis,
                run_id=run_id,
                thread_id=thread_id,
                turn_id=turn.turn_id,
                topic_id=turn.topic_id or "",
                request=request,
                context_manifest=context_manifest,
                turn_intent=turn.turn_intent.intent,
                topic_relation=turn.topic_relation,
                llm_calls=workflow_llm_calls,
            )
        return _finalize_workflow_terminal(
            store=self.store,
            result=result,
            run_id=run_id,
            thread_id=thread_id,
            turn_id=turn.turn_id,
            topic_id=turn.topic_id or "",
            request=request,
            context_manifest=context_manifest,
            intent=turn.turn_intent.intent,
            topic_relation=turn.topic_relation,
            llm_calls=workflow_llm_calls,
        )

    def _run_material_revision_successor(
        self,
        *,
        thread_id: str,
        source_run_id: str,
        artifact_root: str,
        user_id: str | None,
        decision_result: Mapping[str, Any],
        stop_after_phase: str | None,
    ) -> dict[str, Any]:
        try:
            continuation = MaterialRevisionContinuation.from_dict(
                decision_result["material_revision_continuation"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceIntegrityError(
                "material_revision_continuation_invalid"
            ) from exc
        dispatch = decision_result.get("successor_run_dispatch")
        source_terminal = decision_result.get("source_terminal")
        if (
            continuation.source_run_id != source_run_id
            or continuation.thread_id != thread_id
            or not isinstance(dispatch, Mapping)
            or set(dispatch)
            != {
                "dispatch_id",
                "dispatch_owner_id",
                "lease_epoch",
            }
            or dispatch.get("dispatch_id") != continuation.successor_dispatch_id
            or not isinstance(dispatch.get("dispatch_owner_id"), str)
            or not str(dispatch.get("dispatch_owner_id") or "").strip()
            or isinstance(dispatch.get("lease_epoch"), bool)
            or not isinstance(dispatch.get("lease_epoch"), int)
            or int(dispatch["lease_epoch"]) <= 0
            or not isinstance(source_terminal, Mapping)
            or source_terminal.get("status") != "interaction_completed"
            or source_terminal.get("run_id") != source_run_id
        ):
            raise EvidenceIntegrityError("material_revision_continuation_invalid")
        successor_result = self.run_message(
            thread_id=thread_id,
            run_id=continuation.successor_run_id,
            user_message=continuation.successor_user_text,
            user_id=user_id,
            artifact_root=artifact_root,
            run_dispatch=dict(dispatch),
            intent_revision_context=continuation.request_payload[
                "intentRevisionContext"
            ],
            stop_after_phase=stop_after_phase,
        )
        return canonical_value(
            {
                **dict(source_terminal),
                "material_revision_continuation": continuation.to_dict(),
                "successor_run_id": continuation.successor_run_id,
                "successor_status": str(successor_result.get("status") or ""),
            }
        )

    def _resume_authoritative_plan_after_decision(
        self,
        *,
        thread_id: str,
        run_id: str,
        artifact_root: str,
        decision_result: Mapping[str, Any],
        stop_after_phase: str | None,
    ) -> dict[str, Any]:
        run_state = self.store.get_run_state(run_id)
        waiting_request = (
            run_state.get("request") if isinstance(run_state, Mapping) else None
        )
        expected_waiting_fields = {
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
        if (
            not isinstance(run_state, Mapping)
            or str(run_state.get("thread_id") or "") != thread_id
            or str(run_state.get("status") or "") != "waiting_for_clarification"
            or not isinstance(waiting_request, Mapping)
            or set(waiting_request) != expected_waiting_fields
            or waiting_request.get("schema_version")
            != "single-authority-phase02-waiting.v1"
            or waiting_request.get("run_attempt_id") != run_id
            or waiting_request.get("thread_id") != thread_id
            or waiting_request.get("turn_id") != run_state.get("turn_id")
            or waiting_request.get("topic_id") != run_state.get("topic_id")
        ):
            raise EvidenceIntegrityError("single_authority_resume_request_invalid")

        active_revision = self.store.resolve_active_intent_revision(run_id)
        if active_revision is None:
            raise EvidenceIntegrityError("decision_intent_not_active")
        ledger = self.store.load_decision_ledger(active_revision.intent_revision_id)
        try:
            decision_checkpoint = DurableTransition.from_dict(
                decision_result["durable_checkpoint"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceIntegrityError(
                "single_authority_decision_checkpoint_invalid"
            ) from exc
        active_decisions = {
            record.decision_id: record.to_dict() for record in ledger.active_records()
        }
        decision_payload = decision_result.get("decision")
        decision_id = (
            str(decision_payload.get("decision_id") or "")
            if isinstance(decision_payload, Mapping)
            else ""
        )
        prior_ledger_position = waiting_request.get("decision_ledger_position")
        latest_transition_id = self.store.latest_accepted_transition_id(run_id)
        if (
            decision_result.get("status") != "decision_recorded"
            or decision_result.get("run_id") != run_id
            or decision_result.get("intent_revision_id")
            != active_revision.intent_revision_id
            or waiting_request.get("intent_revision_id")
            != active_revision.intent_revision_id
            or isinstance(prior_ledger_position, bool)
            or not isinstance(prior_ledger_position, int)
            or ledger.position != prior_ledger_position + 1
            or decision_checkpoint.run_attempt_id != run_id
            or decision_checkpoint.intent_revision_id
            != active_revision.intent_revision_id
            or decision_checkpoint.decision_ledger_position != ledger.position
            or decision_checkpoint.node_name
            not in {"accept_material_decision", "bind_free_text_decision"}
            or decision_checkpoint.status != "succeeded"
            or decision_checkpoint.acceptance_state != "accepted"
            or decision_checkpoint.next_transition != "compile_authoritative_plan"
            or latest_transition_id != decision_checkpoint.transition_id
            or decision_id not in active_decisions
            or canonical_value(decision_payload)
            != canonical_value(active_decisions.get(decision_id))
        ):
            raise EvidenceIntegrityError("single_authority_decision_resume_mismatch")

        runtime_descriptors = waiting_request.get("runtime_descriptors")
        if not isinstance(runtime_descriptors, Mapping):
            raise EvidenceIntegrityError(
                "single_authority_resume_runtime_descriptors_invalid"
            )
        context_manifest = runtime_descriptors.get("context_manifest")
        persisted_artifact_root = runtime_descriptors.get("artifact_root")
        if (
            runtime_descriptors.get("run_id") != run_id
            or runtime_descriptors.get("run_attempt_id") != run_id
            or runtime_descriptors.get("question") != active_revision.original_user_text
            or not isinstance(persisted_artifact_root, str)
            or not persisted_artifact_root
            or persisted_artifact_root != artifact_root
            or not isinstance(context_manifest, Mapping)
            or not str(context_manifest.get("manifest_id") or "")
            or waiting_request.get("context_manifest_ref")
            != context_manifest.get("manifest_id")
        ):
            raise EvidenceIntegrityError(
                "single_authority_resume_runtime_descriptors_invalid"
            )
        analysis_context = runtime_descriptors.get("analysis_context") or {}
        if not isinstance(analysis_context, Mapping):
            raise EvidenceIntegrityError(
                "single_authority_resume_analysis_context_invalid"
            )
        request: dict[str, Any] = {
            "run_id": run_id,
            "run_attempt_id": run_id,
            "question": active_revision.original_user_text,
            "artifact_root": persisted_artifact_root,
            "analysis_context": canonical_value(analysis_context),
            "context_manifest": canonical_value(context_manifest),
            "authority_store": self.store,
        }
        if stop_after_phase is not None:
            request["stop_after_phase"] = stop_after_phase
        for field in ("recursion_limit",):
            if field in runtime_descriptors:
                request[field] = canonical_value(runtime_descriptors[field])
        if self.conversation_llm_client is not None:
            request["llm_client"] = self.conversation_llm_client
        if _requires_post_execution_runtime(stop_after_phase):
            thread = self.store.get_thread(thread_id)
            request.update(
                self._post_execution_runtime_bindings(
                    owner_ref=str(thread.owner_id or ""),
                    thread_id=thread_id,
                )
            )
        if self.runtime_registry is not None:
            request["runtime_registry"] = self.runtime_registry
        request["release_resolver"] = self.release_resolver or self.store
        if self.analysis_runtime is not None:
            request["analysis_runtime"] = self.analysis_runtime

        turn_id = str(waiting_request["turn_id"])
        topic_id = str(waiting_request["topic_id"])
        turn_intent = str(waiting_request["turn_intent"])
        topic_relation = str(waiting_request["topic_relation"])
        for index, audit in enumerate(decision_result.get("llm_calls") or (), start=1):
            if not isinstance(audit, Mapping):
                raise EvidenceIntegrityError(
                    "single_authority_decision_llm_audit_invalid"
                )
            self.store.add_audit_event(
                "single_authority_decision_llm_call_recorded",
                thread_id=thread_id,
                topic_id=topic_id,
                run_id=run_id,
                ref=str(audit.get("response_id") or "")
                or f"{run_id}:decision-llm-call:{index}",
                payload=dict(audit),
            )
        self.store.upsert_run(
            run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            topic_id=topic_id,
            status="running_workflow",
            request=_persistable_request(request),
        )
        result = self.workflow_runner(_workflow_authority_request(request))
        workflow_llm_calls = tuple(
            dict(call) for call in result.llm_calls if isinstance(call, Mapping)
        )
        self.store.record_run_nodes(run_id, tuple(result.checkpoint_events))
        _record_analysis_performance_profile(
            store=self.store,
            registry=self.runtime_registry,
            run_id=run_id,
            thread_id=thread_id,
            topic_id=topic_id,
            checkpoint_events=tuple(result.checkpoint_events),
        )
        if result.status == "waiting_for_clarification":
            if (
                not isinstance(result.interaction_result, Mapping)
                or result.interaction_result.get("schema_version")
                != "single-authority-phase01.v1"
            ):
                raise EvidenceIntegrityError("single_authority_waiting_result_required")
            return _finalize_single_authority_waiting(
                store=self.store,
                interaction_result=result.interaction_result,
                run_id=run_id,
                thread_id=thread_id,
                turn_id=turn_id,
                topic_id=topic_id,
                request=request,
                context_manifest=context_manifest,
                turn_intent=turn_intent,
                topic_relation=topic_relation,
                llm_calls=workflow_llm_calls,
            )
        if result.status == "planned":
            return _finalize_authoritative_plan(
                store=self.store,
                plan_result=result.plan_result,
                run_id=run_id,
                thread_id=thread_id,
                turn_id=turn_id,
                topic_id=topic_id,
                request=request,
                context_manifest=context_manifest,
                turn_intent=turn_intent,
                topic_relation=topic_relation,
                llm_calls=workflow_llm_calls,
                expected_parent_transition_id=(decision_checkpoint.transition_id),
            )
        if result.status == "evidence_ready":
            return _finalize_capability_execution(
                store=self.store,
                plan_result=result.plan_result,
                execution_result=result.execution_result,
                factor_coverage_plan=result.factor_coverage_plan,
                factor_coverage_result=result.factor_coverage_result,
                investigation_branches=result.investigation_branches,
                investigation_synthesis=result.investigation_synthesis,
                run_id=run_id,
                thread_id=thread_id,
                turn_id=turn_id,
                topic_id=topic_id,
                request=request,
                context_manifest=context_manifest,
                turn_intent=turn_intent,
                topic_relation=topic_relation,
                llm_calls=workflow_llm_calls,
                expected_plan_parent_transition_id=(decision_checkpoint.transition_id),
            )
        return _finalize_workflow_terminal(
            store=self.store,
            result=result,
            run_id=run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            topic_id=topic_id,
            request=request,
            context_manifest=context_manifest,
            intent=turn_intent,
            topic_relation=topic_relation,
            llm_calls=workflow_llm_calls,
            failure_owner="plan_runtime_owner",
        )

    def _post_execution_runtime_bindings(
        self,
        *,
        owner_ref: str,
        thread_id: str,
    ) -> dict[str, Any]:
        if not isinstance(self.store, PostgresConversationStore):
            raise EvidenceIntegrityError("post_execution_postgres_store_required")
        connection = self.store.connection
        if not callable(getattr(connection, "execute", None)):
            raise EvidenceIntegrityError("post_execution_connection_invalid")
        values = {
            "owner_ref": owner_ref,
            "thread_id": thread_id,
            "locale": self.post_execution_locale,
            "destination_ref": f"conversation:{thread_id}",
            "publication_channel": self.publication_channel,
        }
        if any(
            not isinstance(value, str) or not value.strip() or value != value.strip()
            for value in values.values()
        ):
            raise EvidenceIntegrityError("post_execution_runtime_binding_missing")
        if not callable(getattr(self.conversation_llm_client, "invoke_json", None)):
            raise EvidenceIntegrityError("post_execution_llm_client_invalid")
        if not callable(self.delivery_transport):
            raise EvidenceIntegrityError("post_execution_delivery_transport_invalid")
        return {
            **values,
            "authority_connection": connection,
            "delivery_transport": self.delivery_transport,
        }

    @classmethod
    def from_environment(cls) -> "ConversationAgentCore":
        store = PostgresConversationStore.from_env()
        conversation_llm_client = _conversation_llm_from_env(
            circuit_connection=store.connection,
        )
        from bi_agent.runtime.analysis_runtime import AnalysisRuntime

        analysis_runtime = AnalysisRuntime.from_environment(store)
        return cls(
            store,
            conversation_llm_client=conversation_llm_client,
            release_resolver=store,
            analysis_runtime=analysis_runtime,
            runtime_registry=analysis_runtime.registry,
            post_execution_locale="zh-CN",
            publication_channel="gateway",
            delivery_transport=GatewayDeliveryTransport(),
        )


_POST_EXECUTION_TERMINALS = {
    "authority_sealed": {
        "run_status": "authority_sealed",
        "stop_after_phase": "phase04",
        "publication_status": "not_ready",
        "delivery_status": "pending",
    },
    "narrative_ready": {
        "run_status": "narrative_ready",
        "stop_after_phase": "phase05",
        "publication_status": "ready",
        "delivery_status": "persisted",
    },
    "completed": {
        "run_status": "completed",
        "stop_after_phase": None,
        "publication_status": "published",
        "delivery_status": "published",
    },
    "delivery_retryable_failed": {
        "run_status": "completed",
        "stop_after_phase": None,
        "publication_status": "ready",
        "delivery_status": "retryable_failed",
    },
    "delivery_permanently_failed": {
        "run_status": "completed",
        "stop_after_phase": None,
        "publication_status": "ready",
        "delivery_status": "permanently_failed",
    },
    "narrative_failed": {
        "run_status": "completed",
        "stop_after_phases": frozenset({None, "phase05"}),
        "publication_status": "not_ready",
        "delivery_status": "pending",
    },
    "publication_failed": {
        "run_status": "completed",
        "stop_after_phases": frozenset({None, "phase05"}),
        "publication_status": "failed",
        "delivery_status": "pending",
    },
}


def _requires_post_execution_runtime(stop_after_phase: str | None) -> bool:
    return stop_after_phase in {None, "phase04", "phase05"}


def _finalize_workflow_terminal(
    *,
    store: Any,
    result: Any,
    run_id: str,
    thread_id: str,
    turn_id: str,
    topic_id: str,
    request: Mapping[str, Any],
    context_manifest: Mapping[str, Any],
    intent: str,
    topic_relation: str,
    llm_calls: tuple[dict[str, Any], ...],
    failure_owner: str = "workflow_runtime_owner",
) -> dict[str, Any]:
    if result.status == "failed":
        failure_reason = str(result.failure_reason or "")
        if not failure_reason:
            raise EvidenceIntegrityError("workflow_failure_reason_missing")
        if getattr(result, "post_execution_result", None) is not None:
            raise EvidenceIntegrityError("failed_post_execution_result_forbidden")
        _record_workflow_failure_llm_audits(
            store,
            thread_id=thread_id,
            topic_id=topic_id,
            run_id=run_id,
            llm_calls=llm_calls,
        )
        persisted_runtime_request = _persisted_runtime_request(
            store,
            run_id=run_id,
        )
        store.upsert_run(
            run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            topic_id=topic_id,
            status="failed",
            request={
                **persisted_runtime_request,
                "failure_reason": failure_reason,
                "failure_owner": failure_owner,
            },
        )
        store.add_audit_event(
            "workflow_failed",
            thread_id=thread_id,
            topic_id=topic_id,
            run_id=run_id,
            payload={
                "failure_reason": failure_reason,
                "failure_owner": failure_owner,
            },
        )
        return {
            "status": "failed",
            "run_id": run_id,
            "turn_id": turn_id,
            "topic_id": topic_id or None,
            "intent": intent,
            "topic_relation": topic_relation,
            "context_manifest": canonical_value(context_manifest),
            "failure_reason": failure_reason,
            "failure_owner": failure_owner,
        }

    terminal = _POST_EXECUTION_TERMINALS.get(str(result.status or ""))
    raw_post_execution = getattr(result, "post_execution_result", None)
    if terminal is None or type(raw_post_execution) is not PostExecutionWorkflowResult:
        raise EvidenceIntegrityError("post_execution_workflow_result_required")
    post_execution = _validated_internal_post_execution_result(raw_post_execution)
    if (
        result.run_id != run_id
        or post_execution.run_attempt_id != run_id
        or post_execution.status != result.status
        or request.get("stop_after_phase")
        not in terminal.get(
            "stop_after_phases",
            frozenset({terminal.get("stop_after_phase")}),
        )
    ):
        raise EvidenceIntegrityError("post_execution_workflow_result_invalid")
    if post_execution.delivery_status not in {
        None,
        terminal["delivery_status"],
    }:
        raise EvidenceIntegrityError("post_execution_delivery_status_mismatch")
    persisted_runtime_request = _persisted_runtime_request(
        store,
        run_id=run_id,
    )
    coverage_refs = _validated_claim_coverage_refs(
        persisted_runtime_request.get("claim_coverage_refs"),
        checkpoint_ref=post_execution.claim_coverage_checkpoint_ref,
        checkpoint_digest=post_execution.claim_coverage_checkpoint_digest,
        transition_id=post_execution.claim_coverage_transition_id,
    )
    factor_audit, factor_coverage_refs = _validated_factor_coverage_audit(
        plan_payload=getattr(result, "factor_coverage_plan", None),
        result_payload=getattr(result, "factor_coverage_result", None),
        branch_payloads=getattr(result, "investigation_branches", ()),
        synthesis_payload=getattr(result, "investigation_synthesis", None),
        execution=(
            post_execution.semantic_authority_result.authority_bundle_inputs.execution_result
        ),
        authority_context=store.load_authority_context(run_id),
    )

    publication_refs = _safe_post_execution_refs(post_execution)
    analysis_status = (
        "boundary_only"
        if post_execution.authority_bundle.authority_mode == "boundary_only"
        else "complete"
    )
    run_status = str(terminal["run_status"])
    publication_status = str(terminal["publication_status"])
    delivery_status = str(terminal["delivery_status"])
    operational_failure = None
    if post_execution.post_seal_failure_terminal is not None:
        failure = post_execution.post_seal_failure_terminal.failure_record
        operational_failure = {
            "failure_ref": failure.failure_id,
            "layer": failure.layer,
            "kind": failure.kind,
            "retryability": failure.retryability,
            "business_boundary": failure.business_boundary,
        }
    persisted_request = {
        **{
            key: value
            for key, value in persisted_runtime_request.items()
            if key != "operational_failure"
        },
        "claim_coverage_refs": coverage_refs,
        "factor_coverage_refs": factor_coverage_refs,
        "post_execution_status": post_execution.status,
        "analysis_status": analysis_status,
        "publication_status": publication_status,
        "delivery_status": delivery_status,
        "publication_refs": publication_refs,
        **(
            {"operational_failure": operational_failure}
            if operational_failure is not None
            else {}
        ),
    }
    store.upsert_run(
        run_id,
        thread_id=thread_id,
        turn_id=turn_id,
        topic_id=topic_id,
        status=run_status,
        request=persisted_request,
    )
    store.add_audit_event(
        "factor_coverage_settled",
        thread_id=thread_id,
        topic_id=topic_id,
        run_id=run_id,
        ref=factor_coverage_refs["coverage_result_ref"],
        payload=factor_audit,
    )

    response = {
        "status": run_status,
        "post_execution_status": post_execution.status,
        "run_id": run_id,
        "turn_id": turn_id,
        "topic_id": topic_id or None,
        "intent": intent,
        "topic_relation": topic_relation,
        "context_manifest": canonical_value(context_manifest),
        "analysis_status": analysis_status,
        "publication_status": publication_status,
        "delivery_status": delivery_status,
        "publication_refs": publication_refs,
    }
    if post_execution.status == "completed":
        response["customer_publication"] = {
            "customer_publication_ref": post_execution.customer_publication_ref,
            "customer_payload_ref": post_execution.customer_payload_ref,
            "publication_ref": post_execution.publication_ref,
            "outbox_ref": post_execution.outbox_ref,
            "payload": canonical_value(post_execution.customer_payload),
        }
    if operational_failure is not None:
        response["operational_failure"] = operational_failure
    return response


def _validated_internal_post_execution_result(
    result: PostExecutionWorkflowResult,
) -> PostExecutionWorkflowResult:
    try:
        return validate_typed_post_execution_workflow_result(result)
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError("post_execution_workflow_result_invalid") from exc


def _validated_factor_coverage_audit(
    *,
    plan_payload: Mapping[str, Any] | None,
    result_payload: Mapping[str, Any] | None,
    branch_payloads: Sequence[Mapping[str, Any]],
    synthesis_payload: Mapping[str, Any] | None,
    execution: AuthoritativeExecutionResult,
    authority_context: Any,
) -> tuple[dict[str, Any], dict[str, str]]:
    try:
        if (
            not isinstance(plan_payload, Mapping)
            or not isinstance(result_payload, Mapping)
            or isinstance(branch_payloads, (str, bytes))
            or not isinstance(branch_payloads, Sequence)
            or not isinstance(synthesis_payload, Mapping)
        ):
            raise TypeError("factor_coverage_shape")
        plan = FactorCoveragePlan.from_dict(plan_payload)
        result = FactorCoverageResult.from_dict(result_payload, plan=plan)
        item_by_ref = {
            item.coverage_item_ref: item for item in plan.coverage_items
        }
        branches = tuple(
            InvestigationBranch.from_dict(
                payload,
                item=item_by_ref[str(payload.get("coverage_item_ref") or "")],
            )
            for payload in branch_payloads
        )
        synthesis = InvestigationSynthesis.from_dict(
            synthesis_payload,
            plan=plan,
            coverage_result=result,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError("factor_coverage_audit_invalid") from exc
    branch_by_item = {item.coverage_item_ref: item for item in branches}
    if (
        plan.run_attempt_id != execution.run_attempt_id
        or plan.intent_revision_id != execution.intent_revision_id
        or plan.plan_revision_id != execution.plan_revision_id
        or plan.authority_context_ref != execution.authority_context_ref
        or result.execution_result_ref
        != execution.authoritative_execution_result_ref
        or set(branch_by_item) != set(item_by_ref)
        or len(branches) != len(item_by_ref)
        or getattr(authority_context, "authority_context_ref", None)
        != plan.authority_context_ref
        or any(
            branch.snapshot_refs != authority_context.snapshot_refs
            or branch.release_refs != authority_context.release_refs
            for branch in branches
        )
    ):
        raise EvidenceIntegrityError("factor_coverage_audit_invalid")
    audit_payload = {
        "schema_version": "factor-coverage-audit.v1",
        "coverage_plan": plan.to_dict(),
        "coverage_result": result.to_dict(),
        "investigation_branches": [item.to_dict() for item in branches],
        "investigation_synthesis": synthesis.to_dict(),
    }
    refs = {
        "schema_version": "factor-coverage-audit.v1",
        "coverage_plan_ref": plan.coverage_plan_ref,
        "coverage_plan_digest": plan.content_digest,
        "coverage_result_ref": result.coverage_result_ref,
        "coverage_result_digest": result.content_digest,
        "investigation_synthesis_ref": synthesis.synthesis_ref,
        "investigation_synthesis_digest": synthesis.content_digest,
    }
    return audit_payload, refs


def _safe_post_execution_refs(
    result: PostExecutionWorkflowResult,
) -> dict[str, str | None]:
    return {
        "post_execution_result_ref": result.result_ref,
        "post_execution_result_digest": result.content_digest,
        "semantic_authority_result_ref": result.semantic_authority_result_ref,
        "semantic_authority_result_digest": result.semantic_authority_result_digest,
        "authority_bundle_ref": result.authority_bundle_ref,
        "authority_bundle_digest": result.authority_bundle_digest,
        "authority_transition_id": result.authority_transition_id,
        "claim_coverage_checkpoint_ref": (result.claim_coverage_checkpoint_ref),
        "claim_coverage_checkpoint_digest": (result.claim_coverage_checkpoint_digest),
        "claim_coverage_transition_id": result.claim_coverage_transition_id,
        "post_seal_failure_terminal_ref": (result.post_seal_failure_terminal_ref),
        "failure_record_ref": (
            None
            if result.post_seal_failure_terminal is None
            else result.post_seal_failure_terminal.failure_record.failure_id
        ),
        "failure_lifecycle_state_digest": (
            None
            if result.post_seal_failure_terminal is None
            else result.post_seal_failure_terminal.lifecycle_state.content_digest
        ),
        "narrative_workflow_ref": result.narrative_workflow_ref,
        "narrative_workflow_digest": result.narrative_workflow_digest,
        "compose_transition_id": result.compose_transition_id,
        "publication_ref": result.publication_ref,
        "outbox_ref": result.outbox_ref,
        "customer_payload_ref": result.customer_payload_ref,
        "delivery_attempt_ref": result.delivery_attempt_ref,
        "customer_publication_ref": result.customer_publication_ref,
    }


def _finalize_authoritative_plan(
    *,
    store: Any,
    plan_result: Mapping[str, Any] | None,
    run_id: str,
    thread_id: str,
    turn_id: str,
    topic_id: str,
    request: Mapping[str, Any],
    context_manifest: Mapping[str, Any],
    turn_intent: str,
    topic_relation: str,
    llm_calls: tuple[dict[str, Any], ...],
    expected_parent_transition_id: str | None = None,
) -> dict[str, Any]:
    if request.get("stop_after_phase") != "phase02":
        raise EvidenceIntegrityError("planned_terminal_requires_explicit_phase02_stop")
    parsed, plan_result_refs = _validate_persisted_authoritative_plan(
        store=store,
        plan_result=plan_result,
        run_id=run_id,
        llm_calls=llm_calls,
        expected_parent_transition_id=expected_parent_transition_id,
    )
    planner_proposal = parsed.planner_proposal
    proposal_admission = parsed.proposal_admission
    plan_revision = parsed.plan_revision
    store.upsert_run(
        run_id,
        thread_id=thread_id,
        turn_id=turn_id,
        topic_id=topic_id,
        status="planned",
        request={
            **_persistable_request(dict(request)),
            "plan_result_refs": plan_result_refs,
        },
    )
    for index, audit in enumerate(llm_calls, start=1):
        response_id = str(audit.get("response_id") or "")
        store.add_audit_event(
            "authoritative_plan_llm_call_recorded",
            thread_id=thread_id,
            topic_id=topic_id,
            run_id=run_id,
            ref=response_id or f"{run_id}:plan-llm-call:{index}",
            payload=audit,
        )
    store.add_audit_event(
        "authoritative_plan_accepted",
        thread_id=thread_id,
        topic_id=topic_id,
        run_id=run_id,
        ref=plan_revision.plan_revision_id,
        payload={
            **plan_result_refs,
            "plan_digest": plan_revision.content_digest,
            "planner_proposal_digest": planner_proposal.content_digest,
            "proposal_admission_digest": proposal_admission.content_digest,
        },
    )
    return {
        "status": "planned",
        "run_id": run_id,
        "turn_id": turn_id,
        "topic_id": topic_id or None,
        "intent": turn_intent,
        "topic_relation": topic_relation,
        "context_manifest": canonical_value(context_manifest),
        "plan_result": canonical_value(plan_result),
        "llm_calls": list(llm_calls),
    }


def _validate_persisted_authoritative_plan(
    *,
    store: Any,
    plan_result: Mapping[str, Any] | None,
    run_id: str,
    llm_calls: tuple[dict[str, Any], ...],
    expected_parent_transition_id: str | None,
):
    parsed = parse_authoritative_plan_result(
        plan_result,
        expected_run_id=run_id,
        expected_llm_calls=llm_calls,
    )
    transition = parsed.transition
    if (
        expected_parent_transition_id is not None
        and parsed.plan_revision.supersedes_plan_revision_id is None
        and transition.parent_transition_id != expected_parent_transition_id
    ):
        raise EvidenceIntegrityError("single_authority_plan_transition_parent_mismatch")
    persisted_transition = store.load_accepted_transition(
        run_attempt_id=run_id,
        node_name=transition.node_name,
        input_digest=transition.input_digest,
    )
    if (
        store.resolve_active_plan_revision(run_id) != parsed.plan_revision
        or store.load_authority_context(run_id) != parsed.authority_context
        or store.load_planner_proposal(parsed.planner_proposal.planner_proposal_id)
        != parsed.planner_proposal
        or store.load_proposal_admission(
            parsed.proposal_admission.proposal_admission_id
        )
        != parsed.proposal_admission
        or not isinstance(persisted_transition, Mapping)
        or persisted_transition.get("transition") != transition
        or canonical_value(persisted_transition.get("input_payload") or {})
        != canonical_value(parsed.transition_input)
        or canonical_value(persisted_transition.get("output_payload") or {})
        != canonical_value(parsed.transition_output)
    ):
        raise EvidenceIntegrityError("single_authority_plan_persistence_mismatch")
    return parsed, {
        "schema_version": AUTHORITATIVE_PLAN_RESULT_SCHEMA_VERSION,
        "plan_patch_ref": parsed.plan_patch_ref,
        **parsed.authority_refs,
    }


def _finalize_capability_execution(
    *,
    store: Any,
    plan_result: Mapping[str, Any] | None,
    execution_result: Mapping[str, Any] | None,
    factor_coverage_plan: Mapping[str, Any] | None,
    factor_coverage_result: Mapping[str, Any] | None,
    investigation_branches: Sequence[Mapping[str, Any]],
    investigation_synthesis: Mapping[str, Any] | None,
    run_id: str,
    thread_id: str,
    turn_id: str,
    topic_id: str,
    request: Mapping[str, Any],
    context_manifest: Mapping[str, Any],
    turn_intent: str,
    topic_relation: str,
    llm_calls: tuple[dict[str, Any], ...],
    expected_plan_parent_transition_id: str | None = None,
) -> dict[str, Any]:
    if request.get("stop_after_phase") != "phase03":
        raise EvidenceIntegrityError(
            "evidence_ready_terminal_requires_explicit_phase03_stop"
        )
    try:
        parsed_execution = AuthoritativeExecutionResult.from_dict(
            execution_result or {}
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceIntegrityError("authoritative_execution_result_invalid") from exc
    if parsed_execution.run_attempt_id != run_id:
        raise EvidenceIntegrityError("authoritative_execution_result_run_mismatch")
    parsed_plan, plan_result_refs = _validate_persisted_authoritative_plan(
        store=store,
        plan_result=plan_result,
        run_id=run_id,
        llm_calls=llm_calls,
        expected_parent_transition_id=(expected_plan_parent_transition_id),
    )
    if parsed_execution.plan_revision != parsed_plan.plan_revision:
        raise EvidenceIntegrityError("authoritative_execution_plan_mismatch")
    factor_audit, factor_coverage_refs = _validated_factor_coverage_audit(
        plan_payload=factor_coverage_plan,
        result_payload=factor_coverage_result,
        branch_payloads=investigation_branches,
        synthesis_payload=investigation_synthesis,
        execution=parsed_execution,
        authority_context=parsed_plan.authority_context,
    )

    persisted_snapshot = store.load_execution_snapshot(
        parsed_execution.plan_revision_id
    )
    persisted_bundles = tuple(
        store.load_capability_outcome(
            parsed_execution.plan_revision_id,
            bundle[1].task_id,
        )
        for bundle in parsed_execution.capability_outcome_bundles
    )
    persisted_bundles_complete = tuple(
        bundle for bundle in persisted_bundles if bundle is not None
    )
    if (
        persisted_snapshot != parsed_execution.execution_snapshot
        or len(persisted_bundles_complete) != len(persisted_bundles)
        or tuple(
            sorted(
                persisted_bundles_complete,
                key=lambda item: item[1].task_id,
            )
        )
        != tuple(
            sorted(
                parsed_execution.capability_outcome_bundles,
                key=lambda item: item[1].task_id,
            )
        )
    ):
        raise EvidenceIntegrityError("authoritative_execution_persistence_mismatch")

    transition_input = capability_execution_transition_input(
        parsed_execution.plan_revision,
        hard_budget_limit=(parsed_execution.exploration_stop_record.hard_budget_limit),
    )
    transition_output = {
        "execution_snapshot": (parsed_execution.execution_snapshot.to_dict()),
        "exploration_stop_record": (parsed_execution.exploration_stop_record.to_dict()),
    }
    persisted_transition = store.load_accepted_transition(
        run_attempt_id=run_id,
        node_name="execute_capability_dag",
        input_digest=parsed_execution.durable_transition.input_digest,
    )
    persisted_request = _persisted_runtime_request(store, run_id=run_id)
    coverage_refs = _validated_claim_coverage_refs(
        persisted_request.get("claim_coverage_refs"),
        plan_revision_id=parsed_execution.plan_revision_id,
        execution_result_ref=(parsed_execution.authoritative_execution_result_ref),
    )
    latest_transition_id = store.latest_accepted_transition_id(run_id)
    if (
        parsed_execution.durable_transition.parent_transition_id
        != parsed_plan.transition.transition_id
        or not isinstance(persisted_transition, Mapping)
        or persisted_transition.get("transition") != parsed_execution.durable_transition
        or canonical_value(persisted_transition.get("input_payload") or {})
        != canonical_value(transition_input)
        or canonical_value(persisted_transition.get("output_payload") or {})
        != canonical_value(transition_output)
        or latest_transition_id != coverage_refs["accepted_transition_id"]
    ):
        raise EvidenceIntegrityError("authoritative_execution_transition_mismatch")

    execution_result_refs = {
        "schema_version": AUTHORITATIVE_EXECUTION_RESULT_SCHEMA_VERSION,
        "authoritative_execution_result_ref": (
            parsed_execution.authoritative_execution_result_ref
        ),
        "intent_revision_id": parsed_execution.intent_revision_id,
        "authority_context_ref": parsed_execution.authority_context_ref,
        "plan_revision_id": parsed_execution.plan_revision_id,
        "execution_snapshot_ref": parsed_execution.execution_snapshot_ref,
        "stop_ref": parsed_execution.stop_ref,
        "accepted_transition_id": parsed_execution.transition_id,
    }
    store.upsert_run(
        run_id,
        thread_id=thread_id,
        turn_id=turn_id,
        topic_id=topic_id,
        status="evidence_ready",
        request={
            **persisted_request,
            "plan_result_refs": plan_result_refs,
            "execution_result_refs": execution_result_refs,
            "claim_coverage_refs": coverage_refs,
            "factor_coverage_refs": factor_coverage_refs,
        },
    )
    for index, audit in enumerate(llm_calls, start=1):
        response_id = str(audit.get("response_id") or "")
        store.add_audit_event(
            "authoritative_plan_llm_call_recorded",
            thread_id=thread_id,
            topic_id=topic_id,
            run_id=run_id,
            ref=response_id or f"{run_id}:plan-llm-call:{index}",
            payload=audit,
        )
    store.add_audit_event(
        "authoritative_plan_accepted",
        thread_id=thread_id,
        topic_id=topic_id,
        run_id=run_id,
        ref=parsed_execution.plan_revision_id,
        payload={
            **plan_result_refs,
            "plan_digest": parsed_execution.plan_revision.content_digest,
            "planner_proposal_digest": (parsed_plan.planner_proposal.content_digest),
            "proposal_admission_digest": (
                parsed_plan.proposal_admission.content_digest
            ),
        },
    )
    store.add_audit_event(
        "capability_execution_settled",
        thread_id=thread_id,
        topic_id=topic_id,
        run_id=run_id,
        ref=parsed_execution.execution_snapshot_ref,
        payload=execution_result_refs,
    )
    store.add_audit_event(
        "factor_coverage_settled",
        thread_id=thread_id,
        topic_id=topic_id,
        run_id=run_id,
        ref=factor_coverage_refs["coverage_result_ref"],
        payload=factor_audit,
    )
    return {
        "status": "evidence_ready",
        "run_id": run_id,
        "turn_id": turn_id,
        "topic_id": topic_id or None,
        "intent": turn_intent,
        "topic_relation": topic_relation,
        "context_manifest": canonical_value(context_manifest),
        "execution_result": parsed_execution.to_dict(),
        "claim_coverage": coverage_refs,
        "llm_calls": list(llm_calls),
    }


_CLAIM_COVERAGE_REF_FIELDS = {
    "schema_version",
    "source_plan_revision_id",
    "source_execution_result_ref",
    "claim_coverage_checkpoint_ref",
    "claim_coverage_checkpoint_digest",
    "claim_coverage_evaluation_ref",
    "plan_expansion_decision_ref",
    "decision",
    "plan_patch_ref",
    "accepted_transition_id",
}


def _persisted_runtime_request(store: Any, *, run_id: str) -> dict[str, Any]:
    run_state = store.get_run_state(run_id)
    persisted_request = (
        run_state.get("request") if isinstance(run_state, Mapping) else None
    )
    if not isinstance(persisted_request, Mapping):
        raise EvidenceIntegrityError("single_authority_runtime_request_missing")
    return dict(canonical_value(persisted_request))


def _validated_claim_coverage_refs(
    value: Any,
    *,
    plan_revision_id: str | None = None,
    execution_result_ref: str | None = None,
    checkpoint_ref: str | None = None,
    checkpoint_digest: str | None = None,
    transition_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CLAIM_COVERAGE_REF_FIELDS:
        raise EvidenceIntegrityError("claim_coverage_terminal_refs_invalid")
    refs = dict(canonical_value(value))
    digest = refs.get("claim_coverage_checkpoint_digest")
    resolved_checkpoint_ref = refs.get("claim_coverage_checkpoint_ref")
    if (
        refs.get("schema_version") != CLAIM_COVERAGE_CHECKPOINT_SCHEMA_VERSION
        or refs.get("decision") != "seal"
        or refs.get("plan_patch_ref") is not None
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or resolved_checkpoint_ref != f"claim-coverage-checkpoint:sha256:{digest}"
        or any(
            not isinstance(refs.get(field), str) or not str(refs[field]).strip()
            for field in (
                "source_plan_revision_id",
                "source_execution_result_ref",
                "claim_coverage_evaluation_ref",
                "plan_expansion_decision_ref",
                "accepted_transition_id",
            )
        )
        or (
            plan_revision_id is not None
            and refs.get("source_plan_revision_id") != plan_revision_id
        )
        or (
            execution_result_ref is not None
            and refs.get("source_execution_result_ref") != execution_result_ref
        )
        or (checkpoint_ref is not None and resolved_checkpoint_ref != checkpoint_ref)
        or (checkpoint_digest is not None and digest != checkpoint_digest)
        or (
            transition_id is not None
            and refs.get("accepted_transition_id") != transition_id
        )
    ):
        raise EvidenceIntegrityError("claim_coverage_terminal_refs_invalid")
    return refs


def _finalize_single_authority_waiting(
    *,
    store: Any,
    interaction_result: Mapping[str, Any],
    run_id: str,
    thread_id: str,
    turn_id: str,
    topic_id: str,
    request: Mapping[str, Any],
    context_manifest: Mapping[str, Any],
    turn_intent: str,
    topic_relation: str,
    llm_calls: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    active_revision = store.resolve_active_intent_revision(run_id)
    revision_payload = interaction_result.get("intent_revision")
    clarification = interaction_result.get("clarification")
    checkpoint = interaction_result.get("durable_checkpoint")
    ledger = interaction_result.get("decision_ledger")
    if (
        active_revision is None
        or not isinstance(revision_payload, Mapping)
        or canonical_value(active_revision.to_dict())
        != canonical_value(revision_payload)
        or not isinstance(clarification, Mapping)
        or not isinstance(checkpoint, Mapping)
        or not isinstance(ledger, Mapping)
    ):
        raise EvidenceIntegrityError("single_authority_waiting_result_invalid")
    raw_options = clarification.get("options")
    if not isinstance(raw_options, list) or not raw_options:
        raise EvidenceIntegrityError("single_authority_clarification_invalid")
    options: list[ClarificationOption] = []
    for raw_option in raw_options:
        if not isinstance(raw_option, Mapping) or set(raw_option) not in (
            {"option_id", "label", "description", "recommended"},
            {
                "option_id",
                "label",
                "description",
                "recommended",
                "typed_value",
            },
        ):
            raise EvidenceIntegrityError(
                "single_authority_clarification_option_invalid"
            )
        option_id = str(raw_option.get("option_id") or "")
        label = str(raw_option.get("label") or "")
        description = str(raw_option.get("description") or "")
        recommended = raw_option.get("recommended")
        if not option_id or not label or not isinstance(recommended, bool):
            raise EvidenceIntegrityError(
                "single_authority_clarification_option_invalid"
            )
        options.append(
            ClarificationOption(
                option_id=option_id,
                label=label,
                description=description,
                recommended=recommended,
            )
        )
    clarification_state = ClarificationState(
        run_id=run_id,
        topic_id=topic_id,
        question=str(clarification.get("question") or ""),
        options=options,
    )
    authority_refs = interaction_result.get("authority_refs") or {}
    waiting_request = {
        "schema_version": "single-authority-phase02-waiting.v1",
        "run_attempt_id": run_id,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "topic_id": topic_id,
        "turn_intent": turn_intent,
        "topic_relation": topic_relation,
        "intent_revision_id": active_revision.intent_revision_id,
        "decision_ledger_position": int(ledger.get("position") or 0),
        "accepted_transition_id": str(
            authority_refs.get("accepted_transition_id") or ""
        ),
        "clarification": canonical_value(clarification),
        "context_manifest_ref": str(context_manifest.get("manifest_id") or ""),
        "runtime_descriptors": _persistable_request(dict(request)),
    }
    store.finalize_waiting_clarification(
        run_id=run_id,
        thread_id=thread_id,
        turn_id=turn_id,
        topic_id=topic_id,
        request=waiting_request,
        clarification_state=clarification_state,
    )
    return {
        "status": "waiting_for_clarification",
        "run_id": run_id,
        "turn_id": turn_id,
        "topic_id": topic_id,
        "intent": turn_intent,
        "topic_relation": topic_relation,
        "context_manifest": canonical_value(context_manifest),
        "clarification": canonical_value(clarification),
        "intent_revision": canonical_value(revision_payload),
        "decision_ledger": canonical_value(ledger),
        "durable_checkpoint": canonical_value(checkpoint),
        "raw_intent_output": canonical_value(
            interaction_result.get("raw_intent_output") or {}
        ),
        "raw_clarification_output": canonical_value(
            interaction_result.get("raw_clarification_output") or {}
        ),
        "interaction_result": canonical_value(interaction_result),
        "llm_calls": list(llm_calls),
    }


def _validated_topic_selection_submission(
    *,
    store: Any,
    thread_id: str,
    topic_selection: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    if not isinstance(topic_selection, Mapping) or set(topic_selection) != {
        "source_run_id",
        "topic_id",
    }:
        raise EvidenceIntegrityError("topic_selection_envelope_invalid")
    source_run_id = topic_selection.get("source_run_id")
    selected_topic_id = topic_selection.get("topic_id")
    if (
        not isinstance(source_run_id, str)
        or not source_run_id.strip()
        or source_run_id != source_run_id.strip()
        or not isinstance(selected_topic_id, str)
        or not selected_topic_id.strip()
        or selected_topic_id != selected_topic_id.strip()
    ):
        raise EvidenceIntegrityError("topic_selection_envelope_invalid")
    interaction, context = _validated_topic_choice_source(
        store=store,
        thread_id=thread_id,
        source_run_id=source_run_id,
    )
    option_ids = {option.topic_id for option in interaction.options}
    if selected_topic_id not in option_ids:
        raise EvidenceIntegrityError("topic_selection_option_invalid")
    topic = store.topic(selected_topic_id)
    if topic is None or topic.thread_id != thread_id:
        raise EvidenceIntegrityError("topic_selection_option_invalid")
    source_user_message = context.get("source_user_message")
    intent = context.get("intent")
    business_summary = context.get("business_summary")
    confidence = context.get("confidence")
    return source_user_message, {
        "schema_version": "persisted-topic-selection.v1",
        "source_run_id": source_run_id,
        "intent": intent,
        "confidence": float(confidence),
        "business_summary": business_summary,
        "selected_topic_id": selected_topic_id,
    }


def _validated_topic_choice_answer_submission(
    *,
    store: Any,
    thread_id: str,
    user_message: str,
    topic_choice_answer: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    if not isinstance(topic_choice_answer, Mapping) or set(topic_choice_answer) != {
        "source_run_id",
        "answer",
    }:
        raise EvidenceIntegrityError("topic_choice_answer_envelope_invalid")
    source_run_id = topic_choice_answer.get("source_run_id")
    answer = topic_choice_answer.get("answer")
    if (
        not isinstance(source_run_id, str)
        or not source_run_id.strip()
        or source_run_id != source_run_id.strip()
        or not isinstance(answer, str)
        or not answer.strip()
        or answer != answer.strip()
        or answer != user_message.strip()
    ):
        raise EvidenceIntegrityError("topic_choice_answer_envelope_invalid")
    interaction, context = _validated_topic_choice_source(
        store=store,
        thread_id=thread_id,
        source_run_id=source_run_id,
    )
    return answer, {
        "schema_version": "pending-topic-choice.v1",
        "source_run_id": source_run_id,
        "source_user_message": context["source_user_message"],
        "options": [option.to_dict() for option in interaction.options],
    }


def _validated_topic_choice_source(
    *,
    store: Any,
    thread_id: str,
    source_run_id: str,
) -> tuple[TopicChoiceInteractionResponse, Mapping[str, Any]]:
    get_run_state = getattr(store, "get_run_state", None)
    if not callable(get_run_state):
        raise EvidenceIntegrityError("topic_selection_source_resolver_missing")
    source_run = get_run_state(source_run_id)
    if (
        not isinstance(source_run, Mapping)
        or source_run.get("status") != "interaction_completed"
        or source_run.get("thread_id") != thread_id
    ):
        raise EvidenceIntegrityError("topic_selection_source_invalid")
    source_request = source_run.get("request")
    if not isinstance(source_request, Mapping) or set(source_request) != {
        "interaction_result",
        "interaction_context",
        "conversation_entry",
    }:
        raise EvidenceIntegrityError("topic_selection_source_invalid")
    entry = source_request.get("conversation_entry")
    if (
        not isinstance(entry, Mapping)
        or entry.get("schema_version") != "conversation-entry-command.v1"
        or entry.get("run_attempt_id") != source_run_id
        or entry.get("thread_id") != thread_id
        or not isinstance(entry.get("command_digest"), str)
        or len(entry["command_digest"]) != 64
        or not isinstance(entry.get("accepted_attempt_ref"), str)
        or not entry["accepted_attempt_ref"]
    ):
        raise EvidenceIntegrityError("topic_selection_source_invalid")
    try:
        interaction = TopicChoiceInteractionResponse.from_dict(
            source_request["interaction_result"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError("topic_selection_source_invalid") from exc
    context = source_request.get("interaction_context")
    if not isinstance(context, Mapping) or set(context) != {
        "schema_version",
        "source_user_message",
        "intent",
        "confidence",
        "business_summary",
    }:
        raise EvidenceIntegrityError("topic_selection_context_invalid")
    source_user_message = context.get("source_user_message")
    intent = context.get("intent")
    business_summary = context.get("business_summary")
    confidence = context.get("confidence")
    if (
        context.get("schema_version") != "topic-choice-context.v1"
        or interaction.intent != intent
        or not isinstance(source_user_message, str)
        or not source_user_message.strip()
        or source_user_message != source_user_message.strip()
        or not isinstance(business_summary, str)
        or not business_summary.strip()
        or business_summary != business_summary.strip()
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise EvidenceIntegrityError("topic_selection_context_invalid")
    return interaction, context


def _topic_choice_analysis_question(
    *,
    source_user_message: str,
    answer: str,
) -> str:
    return f"原始业务问题：{source_user_message}\n用户对话题归属的补充：{answer}"


def _is_single_authority_clarification_submission(
    *,
    clarification: Mapping[str, Any] | None,
    run_id: str,
    user_message: str,
) -> bool:
    if not clarification:
        return False
    expected_keys = {
        "sourceRunId",
        "resolutionId",
        "attemptRunId",
        "answer",
        "selectedOptionId",
        "source",
        "retryAttempt",
    }
    if set(clarification) != expected_keys:
        raise EvidenceIntegrityError("clarification_attempt_envelope_invalid")
    selected_option_id = clarification.get("selectedOptionId")
    if selected_option_id is not None and (
        not isinstance(selected_option_id, str) or not selected_option_id.strip()
    ):
        raise EvidenceIntegrityError("clarification_attempt_envelope_invalid")
    if (
        str(clarification.get("sourceRunId") or "").strip() != run_id
        or str(clarification.get("attemptRunId") or "").strip() != run_id
        or str(clarification.get("answer") or "").strip() != user_message.strip()
        or clarification.get("source") != "user"
        or clarification.get("retryAttempt") is not False
        or not str(clarification.get("resolutionId") or "").strip()
    ):
        raise EvidenceIntegrityError("clarification_attempt_envelope_invalid")
    return True


def _record_single_authority_clarification_submission(
    *,
    store: Any,
    llm_client: Any,
    thread_id: str,
    run_id: str,
    user_message: str,
    clarification: Mapping[str, Any],
) -> dict[str, Any]:
    run_state = store.get_run_state(run_id)
    if (
        not isinstance(run_state, Mapping)
        or str(run_state.get("thread_id") or "") != thread_id
        or str(run_state.get("status") or "") != "waiting_for_clarification"
    ):
        raise EvidenceIntegrityError("clarification_source_not_waiting")
    active_revision = store.resolve_active_intent_revision(run_id)
    if active_revision is None:
        raise EvidenceIntegrityError("decision_intent_not_active")
    selected_option_id = clarification.get("selectedOptionId")
    llm_calls: list[dict[str, Any]] = []
    raw_binding: dict[str, Any] = {}
    if isinstance(selected_option_id, str):
        accepted = store.accept_decision_option(
            run_attempt_id=run_id,
            option_id=selected_option_id.strip(),
            source="user",
        )
    else:
        accepted, raw_binding, audit = _bind_single_authority_free_text(
            store=store,
            llm_client=llm_client,
            thread_id=thread_id,
            run_id=run_id,
            active_revision=active_revision,
            user_message=user_message,
        )
        if audit is not None:
            llm_calls.append(audit)
        if accepted["status"] != "decision_recorded":
            store.add_audit_event(
                "single_authority_directive_recorded",
                thread_id=thread_id,
                topic_id=str(run_state.get("topic_id") or ""),
                run_id=run_id,
                ref=str(accepted["directive"]["directive_id"]),
                payload={
                    "kind": accepted["directive"]["kind"],
                    "accepted_transition_id": accepted["durable_checkpoint"][
                        "transition_id"
                    ],
                    "replayed": accepted["replayed"],
                },
            )
            directive_result = {
                **{
                    key: value
                    for key, value in accepted.items()
                    if key not in {"source_terminal", "source_waiting"}
                },
                "run_id": run_id,
                "turn_id": str(run_state.get("turn_id") or ""),
                "topic_id": str(run_state.get("topic_id") or "") or None,
                "intent_revision_id": active_revision.intent_revision_id,
                "raw_decision_binding": canonical_value(raw_binding),
                "llm_calls": llm_calls,
            }
            if accepted["status"] != "run_cancelled":
                return directive_result
            source_terminal = accepted.get("source_terminal")
            interaction_result = (
                source_terminal.get("interaction_result")
                if isinstance(source_terminal, Mapping)
                else None
            )
            if (
                not isinstance(source_terminal, Mapping)
                or source_terminal.get("status") != "interaction_completed"
                or source_terminal.get("run_id") != run_id
                or source_terminal.get("intent") != "analysis_cancellation"
                or source_terminal.get("topic_relation") != "analysis_cancellation"
                or not isinstance(source_terminal.get("context_manifest"), Mapping)
                or not isinstance(interaction_result, Mapping)
                or interaction_result.get("schema_version") != "typed-interaction.v1"
                or interaction_result.get("intent") != "analysis_cancellation"
                or not str(interaction_result.get("response_text") or "").strip()
            ):
                raise EvidenceIntegrityError("analysis_cancellation_terminal_invalid")
            return {
                **directive_result,
                **canonical_value(source_terminal),
            }
    ledger = store.load_decision_ledger(active_revision.intent_revision_id)
    store.clear_pending_clarification(thread_id)
    store.add_audit_event(
        "single_authority_decision_recorded",
        thread_id=thread_id,
        topic_id=str(run_state.get("topic_id") or ""),
        run_id=run_id,
        ref=str(accepted["decision"]["decision_id"]),
        payload={
            "intent_revision_id": active_revision.intent_revision_id,
            "option_id": str(accepted["decision"].get("option_id") or ""),
            "decision_ledger_position": accepted["decision_ledger_position"],
            "accepted_transition_id": accepted["durable_checkpoint"]["transition_id"],
            "replayed": accepted["replayed"],
        },
    )
    return {
        "status": "decision_recorded",
        "run_id": run_id,
        "turn_id": str(run_state.get("turn_id") or ""),
        "topic_id": str(run_state.get("topic_id") or "") or None,
        "intent_revision_id": active_revision.intent_revision_id,
        "decision": canonical_value(accepted["decision"]),
        "decision_ledger": {
            "position": ledger.position,
            "records": [record.to_dict() for record in ledger.records],
        },
        "durable_checkpoint": canonical_value(accepted["durable_checkpoint"]),
        "replayed": bool(accepted["replayed"]),
        "raw_decision_binding": canonical_value(raw_binding),
        "llm_calls": llm_calls,
    }


def _temporal_option_value_ref(
    option: Mapping[str, Any],
    *,
    time_spec: Mapping[str, Any],
) -> str:
    try:
        _, value_ref = normalize_temporal_decision_value(
            slot_id=str(option.get("slot_id") or ""),
            value=option.get("typed_value"),
            time_spec=time_spec,
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceIntegrityError("decision_option_typed_value_invalid") from exc
    return value_ref


def _bind_single_authority_free_text(
    *,
    store: Any,
    llm_client: Any,
    thread_id: str,
    run_id: str,
    active_revision: Any,
    user_message: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    if llm_client is None or not callable(getattr(llm_client, "invoke_json", None)):
        raise EvidenceIntegrityError("free_text_binding_llm_missing")
    attempt_journal = getattr(store, "attempt_journal", None)
    if not isinstance(attempt_journal, DurableCallJournal):
        raise EvidenceIntegrityError("free_text_binding_journal_missing")
    replayed = store.load_accepted_free_text_submission(
        run_attempt_id=run_id,
        original_user_text=user_message,
    )
    if replayed is not None:
        try:
            attempt_journal.load_stage_attempt_refs(
                run_attempt_id=run_id,
                transition_attempt_id=replayed["transition"].attempt_id,
                stage_name="bind_free_text_submission",
            )
        except DurableCallJournalError as exc:
            raise EvidenceIntegrityError(
                "free_text_binding_stage_seal_invalid"
            ) from exc
        output_payload = replayed.get("output_payload") or {}
        raw_binding = dict(output_payload.get("raw_provider_output") or {})
        if "decision" in output_payload:
            return (
                {
                    "status": "decision_recorded",
                    "decision": canonical_value(output_payload["decision"]),
                    "decision_ledger_position": replayed[
                        "transition"
                    ].decision_ledger_position,
                    "durable_checkpoint": replayed["transition"].to_dict(),
                    "replayed": True,
                },
                raw_binding,
                None,
            )
        directive = output_payload.get("directive")
        if not isinstance(directive, Mapping):
            raise EvidenceIntegrityError("free_text_binding_checkpoint_invalid")
        continuation = None
        if str(directive["kind"]) == "material_intent_change":
            try:
                continuation = MaterialRevisionContinuation.create(
                    directive=InteractionDirective.from_dict(directive),
                    thread_id=thread_id,
                    successor_user_text=str(
                        raw_binding.get("replacement_user_text") or ""
                    ),
                    superseded_plan_fields=(
                        _affected_plan_fields_for_binding_fields(
                            raw_binding.get("affected_binding_fields") or ()
                        )
                    ),
                    parent_transition_id=replayed["transition"].transition_id,
                )
            except ValueError as exc:
                raise EvidenceIntegrityError(
                    "material_revision_continuation_invalid"
                ) from exc
        return (
            {
                "status": _directive_result_status(str(directive["kind"])),
                "directive": canonical_value(directive),
                "replacement_user_text": str(
                    raw_binding.get("replacement_user_text") or ""
                ),
                "superseded_plan_fields": list(
                    _affected_plan_fields_for_binding_fields(
                        raw_binding.get("affected_binding_fields") or ()
                    )
                ),
                "durable_checkpoint": replayed["transition"].to_dict(),
                "replayed": True,
                **(
                    {"material_revision_continuation": continuation.to_dict()}
                    if continuation is not None
                    else {}
                ),
            },
            raw_binding,
            None,
        )

    ledger = store.load_decision_ledger(active_revision.intent_revision_id)
    options = store.load_decision_options(active_revision.intent_revision_id)
    active_decisions = [decision.to_dict() for decision in ledger.active_records()]
    challenge_target_refs = [
        active_revision.intent_revision_id,
        *(item["decision_id"] for item in active_decisions),
    ]
    prompt_payload = {
        "original_user_text": user_message,
        "active_intent_revision": active_revision.to_dict(),
        "active_decisions": active_decisions,
        "ambiguity_slots": [
            canonical_value(slot) for slot in active_revision.ambiguity_slots
        ],
        "allowed_slot_values": [
            {
                "slot_id": str(option.get("slot_id") or ""),
                "value_ref": _temporal_option_value_ref(
                    option,
                    time_spec=active_revision.time_spec,
                ),
                "business_label": str(option.get("display_label") or ""),
            }
            for option in options
        ],
        "challenge_target_refs": challenge_target_refs,
        "material_binding_field_catalog": [
            "goal_bindings",
            "target_metric_refs",
            "time_spec",
            "comparison_spec",
            "scope",
            "direction_premise",
            "requested_analysis_axes",
            "desired_decisions",
        ],
    }
    spec = build_prompt("single_authority_decision_binding", prompt_payload)

    def validate(candidate: Mapping[str, Any]) -> None:
        _validated_single_authority_decision_binding(
            candidate,
            active_revision=active_revision,
            ledger=ledger,
            options=options,
            challenge_target_refs=frozenset(challenge_target_refs),
        )

    durable_client = DurableProviderClient(
        llm_client,
        journal=attempt_journal,
        run_attempt_id=run_id,
        intent_revision_id=active_revision.intent_revision_id,
        plan_revision_id=None,
        call_kind="clarification_provider",
        task_id=None,
        stage_name="bind_free_text_submission",
    )
    invoke_kwargs: dict[str, Any] = {
        "task": spec.task,
        "prompt_version": spec.prompt_version,
        "messages": spec.messages,
        "required_keys": spec.required_keys,
        "output_validator": validate,
    }
    if durable_client.supports_model_tier:
        invoke_kwargs["model_tier"] = "critical"
    if durable_client.supports_thinking_mode:
        invoke_kwargs["thinking"] = "enabled"
    result = durable_client.invoke_json(**invoke_kwargs)
    accepted_attempt_refs = durable_client.accepted_attempt_refs
    if len(accepted_attempt_refs) != 1:
        raise EvidenceIntegrityError("free_text_binding_attempt_cardinality_invalid")
    raw_binding = dict(result.output)
    validate(raw_binding)
    kind = str(raw_binding["binding_kind"])
    if kind in {"fill_current_slot", "revise_current_slot"}:
        accepted = store.record_typed_slot_decision(
            run_attempt_id=run_id,
            slot_id=str(raw_binding["slot_id"]),
            value_ref=str(raw_binding["value_ref"]),
            original_user_text=user_message,
            binding_kind=kind,
            provider_ref=str(result.audit.get("provider") or "llm_provider"),
            model_ref=str(result.audit.get("model") or "configured-model"),
            raw_provider_output=raw_binding,
            accepted_attempt_refs=accepted_attempt_refs,
        )
        return (
            {"status": "decision_recorded", **accepted},
            raw_binding,
            dict(result.audit),
        )

    directive_kind = {
        "material_intent_change": "material_intent_change",
        "cancel": "cancel",
        "challenge": "challenge",
    }[kind]
    directive = InteractionDirective.create(
        run_attempt_id=run_id,
        intent_revision_id=active_revision.intent_revision_id,
        kind=directive_kind,
        target_refs=tuple(raw_binding["target_refs"]),
        original_user_text=user_message,
    )
    input_payload = {
        "original_user_text": user_message,
        "intent_revision_id": active_revision.intent_revision_id,
        "binding_kind": kind,
    }
    output_payload = {
        "directive": directive.to_dict(),
        "raw_provider_output": canonical_value(raw_binding),
    }
    transition = DurableTransition.create(
        node_name="bind_free_text_directive",
        parent_transition_id=store.latest_accepted_transition_id(run_id),
        run_attempt_id=run_id,
        intent_revision_id=active_revision.intent_revision_id,
        decision_ledger_position=ledger.position,
        input_digest=canonical_digest(input_payload),
        output_digest=canonical_digest(output_payload),
        execution_attempt=1,
        provider_ref=str(result.audit.get("provider") or "llm_provider"),
        model_ref=str(result.audit.get("model") or "configured-model"),
        status="succeeded",
        acceptance_state="accepted",
        next_transition={
            "material_intent_change": "create_intent_revision",
            "cancel": "cancelled",
            "challenge": "repair_scope_pending",
        }[kind],
    )
    continuation = None
    if directive_kind == "material_intent_change":
        try:
            continuation = MaterialRevisionContinuation.create(
                directive=directive,
                thread_id=thread_id,
                successor_user_text=str(raw_binding["replacement_user_text"]),
                superseded_plan_fields=(
                    _affected_plan_fields_for_binding_fields(
                        raw_binding["affected_binding_fields"]
                    )
                ),
                parent_transition_id=transition.transition_id,
            )
        except ValueError as exc:
            raise EvidenceIntegrityError(
                "material_revision_continuation_invalid"
            ) from exc
    saved = store.save_interaction_directive_transition(
        directive=directive,
        transition=transition,
        input_payload=input_payload,
        output_payload=output_payload,
        accepted_attempt_refs=accepted_attempt_refs,
        material_revision_continuation=continuation,
    )
    return (
        {
            "status": _directive_result_status(directive_kind),
            **saved,
            "replacement_user_text": str(
                raw_binding.get("replacement_user_text") or ""
            ),
            "superseded_plan_fields": list(
                _affected_plan_fields_for_binding_fields(
                    raw_binding.get("affected_binding_fields") or ()
                )
            ),
        },
        raw_binding,
        dict(result.audit),
    )


def _validated_single_authority_decision_binding(
    candidate: Mapping[str, Any],
    *,
    active_revision: Any,
    ledger: Any,
    options: tuple[Mapping[str, Any], ...],
    challenge_target_refs: frozenset[str],
) -> None:
    expected = {
        "binding_kind",
        "slot_id",
        "value_ref",
        "target_refs",
        "affected_binding_fields",
        "replacement_user_text",
        "status_message",
    }
    if not isinstance(candidate, Mapping) or set(candidate) != expected:
        raise ValueError("free_text_binding_shape_invalid")
    kind = candidate.get("binding_kind")
    if kind not in {
        "fill_current_slot",
        "revise_current_slot",
        "material_intent_change",
        "cancel",
        "challenge",
    }:
        raise ValueError("free_text_binding_kind_invalid")
    for field in ("slot_id", "value_ref", "replacement_user_text", "status_message"):
        if not isinstance(candidate.get(field), str):
            raise ValueError("free_text_binding_scalar_invalid")
    if not str(candidate["status_message"]).strip():
        raise ValueError("free_text_binding_status_message_invalid")
    target_refs = candidate.get("target_refs")
    if not isinstance(target_refs, list) or any(
        not isinstance(item, str) or not item for item in target_refs
    ):
        raise ValueError("free_text_binding_target_refs_invalid")
    affected_binding_fields = candidate.get("affected_binding_fields")
    known_binding_fields = {
        "goal_bindings",
        "target_metric_refs",
        "time_spec",
        "comparison_spec",
        "scope",
        "direction_premise",
        "requested_analysis_axes",
        "desired_decisions",
    }
    if (
        not isinstance(affected_binding_fields, list)
        or any(
            not isinstance(item, str) or item not in known_binding_fields
            for item in affected_binding_fields
        )
        or len(set(affected_binding_fields)) != len(affected_binding_fields)
    ):
        raise ValueError("free_text_binding_affected_fields_invalid")
    slot_id = str(candidate["slot_id"])
    value_ref = str(candidate["value_ref"])
    replacement = str(candidate["replacement_user_text"])
    if kind in {"fill_current_slot", "revise_current_slot"}:
        known_values = {
            (
                str(item.get("slot_id") or ""),
                _temporal_option_value_ref(
                    item,
                    time_spec=active_revision.time_spec,
                ),
            )
            for item in options
        }
        if (
            (slot_id, value_ref) not in known_values
            or target_refs
            or affected_binding_fields
            or replacement
        ):
            raise ValueError("free_text_binding_slot_value_invalid")
        prior = ledger.active_for_slot(slot_id)
        if kind == "fill_current_slot" and prior is not None:
            raise ValueError("free_text_binding_slot_already_resolved")
        if kind == "revise_current_slot" and prior is None:
            raise ValueError("free_text_binding_revision_missing_prior")
        return
    if slot_id or value_ref:
        raise ValueError("free_text_binding_non_slot_fields_invalid")
    if kind == "cancel":
        if target_refs or affected_binding_fields or replacement:
            raise ValueError("free_text_binding_cancel_invalid")
        return
    if kind == "challenge":
        if (
            replacement
            or affected_binding_fields
            or not target_refs
            or not set(target_refs).issubset(challenge_target_refs)
        ):
            raise ValueError("free_text_binding_challenge_invalid")
        return
    if (
        target_refs != [active_revision.intent_revision_id]
        or not replacement.strip()
        or not affected_binding_fields
    ):
        raise ValueError("free_text_binding_material_change_invalid")


def _directive_result_status(kind: str) -> str:
    return {
        "material_intent_change": "material_revision_required",
        "cancel": "run_cancelled",
        "challenge": "challenge_recorded",
    }[kind]


def _affected_plan_fields_for_binding_fields(
    binding_fields: Any,
) -> tuple[str, ...]:
    mapping = {
        "goal_bindings": (
            "goal_bindings",
            "desired_decisions",
            "analysis_axes",
        ),
        "target_metric_refs": (
            "target_metric_refs",
            "analysis_axes",
            "baseline_refs",
            "resolved_window_refs",
        ),
        "time_spec": (
            "time_spec",
            "baseline_refs",
            "resolved_window_refs",
        ),
        "comparison_spec": (
            "baseline_refs",
            "resolved_window_refs",
        ),
        "scope": ("scope", "filters"),
        "direction_premise": ("direction_premise",),
        "requested_analysis_axes": ("analysis_axes",),
        "desired_decisions": ("desired_decisions",),
    }
    return tuple(
        dict.fromkeys(
            plan_field
            for binding_field in binding_fields
            for plan_field in mapping.get(str(binding_field), ())
        )
    )


def _record_workflow_failure_llm_audits(
    store: Any,
    *,
    thread_id: str,
    topic_id: str,
    run_id: str,
    llm_calls: tuple[dict[str, Any], ...],
    start_index: int = 1,
) -> None:
    for index, audit in enumerate(llm_calls, start=start_index):
        response_id = str(audit.get("response_id") or "")
        store.add_audit_event(
            "workflow_failure_llm_call_recorded",
            thread_id=thread_id,
            topic_id=topic_id,
            run_id=run_id,
            ref=response_id or f"{run_id}:llm-call:{index}",
            payload=audit,
        )


def _conversation_llm_from_env(*, circuit_connection: Any = None) -> Any:
    from bi_agent.runtime.llm_client import LLMConfigurationError
    from bi_agent.runtime.mainland_model_provider import MainlandModelProvider

    try:
        return MainlandModelProvider.structured_client_from_env(
            circuit_connection=circuit_connection,
        )
    except LLMConfigurationError:
        raise
    except Exception as exc:
        raise LLMConfigurationError("llm_client_initialization_failed") from exc


def _validated_analysis_context(
    value: Mapping[str, Any] | None,
) -> dict[str, str]:
    if value is None:
        return {}
    allowed = {
        "as_of",
        "target_date",
        "previous_day",
        "rolling_7_day_start",
        "rolling_7_day_end",
        "same_weekday_last_week",
        "pattern_history_start",
        "anomaly_history_start",
    }
    if (
        not isinstance(value, Mapping)
        or "as_of" not in value
        or not set(value).issubset(allowed)
    ):
        raise PermissionError("analysis_context_external_override_rejected")
    raw = value.get("as_of")
    if not isinstance(raw, str):
        raise ValueError("analysis_context_as_of_invalid")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("analysis_context_as_of_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("analysis_context_as_of_timezone_required")
    normalized = {"as_of": parsed.isoformat()}
    for key in allowed - {"as_of"}:
        if key not in value:
            continue
        raw_date = value[key]
        if not isinstance(raw_date, str):
            raise ValueError(f"analysis_context_{key}_invalid")
        try:
            normalized[key] = date.fromisoformat(raw_date).isoformat()
        except ValueError as exc:
            raise ValueError(f"analysis_context_{key}_invalid") from exc
    return normalized


def _validated_intent_revision_context(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if value is None:
        return {}
    expected = {
        "supersedes_intent_revision_id",
        "superseded_plan_fields",
        "intent_revision_reason_ref",
        "parent_transition_id",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("intent_revision_context_invalid")
    for key in (
        "supersedes_intent_revision_id",
        "intent_revision_reason_ref",
        "parent_transition_id",
    ):
        if not isinstance(value.get(key), str) or not str(value[key]).strip():
            raise ValueError("intent_revision_context_invalid")
    raw_fields = value.get("superseded_plan_fields")
    allowed_fields = {
        "goal_bindings",
        "desired_decisions",
        "analysis_axes",
        "target_metric_refs",
        "baseline_refs",
        "resolved_window_refs",
        "time_spec",
        "scope",
        "filters",
        "direction_premise",
    }
    if (
        not isinstance(raw_fields, list)
        or not raw_fields
        or any(
            not isinstance(item, str) or item not in allowed_fields
            for item in raw_fields
        )
        or len(set(raw_fields)) != len(raw_fields)
    ):
        raise ValueError("intent_revision_context_invalid")
    return {
        "supersedes_intent_revision_id": str(value["supersedes_intent_revision_id"]),
        "superseded_plan_fields": list(raw_fields),
        "intent_revision_reason_ref": str(value["intent_revision_reason_ref"]),
        "parent_transition_id": str(value["parent_transition_id"]),
    }


def _conversation_entry_failure_reason(exc: Exception) -> str:
    if isinstance(
        exc,
        (ConversationOrchestrationError, EvidenceIntegrityError),
    ):
        reason = str(exc).strip()
        if re.fullmatch(r"[a-z][a-z0-9_]*(?::[a-z0-9_,.=-]+)*", reason):
            return reason
    return "conversation_orchestration_failed"


def _workflow_authority_request(request: Mapping[str, Any]) -> dict[str, Any]:
    fields = workflow_request_fields(request.get("stop_after_phase"))
    return {key: value for key, value in request.items() if key in fields}


def _persistable_request(request: dict[str, Any]) -> dict[str, Any]:
    safe = dict(request)
    for key in (
        "llm_client",
        "runtime_registry",
        "release_resolver",
        "analysis_runtime",
        "authority_store",
        "authority_connection",
        "delivery_transport",
    ):
        if key in safe:
            safe[key] = _runtime_object_descriptor(safe[key])
    return safe


def _runtime_object_descriptor(value: Any) -> dict[str, str]:
    return {
        "type": value.__class__.__name__,
        "module": value.__class__.__module__,
    }


AGENT_CORE_COMMAND_MAX_BYTES = 128 * 1024
AGENT_CORE_MESSAGE_MAX_BYTES = 16 * 1024


def _agent_core_command(raw: bytes) -> Mapping[str, Any]:
    if len(raw) > AGENT_CORE_COMMAND_MAX_BYTES:
        raise ValueError("agent_core_command_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("agent_core_command_malformed_json") from exc
    if not isinstance(value, Mapping):
        raise ValueError("agent_core_command_shape_invalid")
    required = {"threadId", "runId", "message", "userId"}
    allowed = required | {
        "artifactRoot",
        "clarification",
        "runDispatch",
        "asOf",
        "intentRevisionContext",
        "topicSelection",
        "topicChoiceAnswer",
        "stopAfterPhase",
    }
    if set(value) - allowed or not required.issubset(value):
        raise ValueError("agent_core_command_shape_invalid")
    for field in required:
        field_value = value.get(field)
        if (
            not isinstance(field_value, str)
            or not field_value
            or field_value != field_value.strip()
        ):
            raise ValueError("agent_core_command_text_invalid")
    if len(str(value["message"]).encode("utf-8")) > AGENT_CORE_MESSAGE_MAX_BYTES:
        raise ValueError("agent_core_message_too_large")
    for field in (
        "clarification",
        "runDispatch",
        "intentRevisionContext",
        "topicSelection",
        "topicChoiceAnswer",
    ):
        if field in value and not isinstance(value[field], Mapping):
            raise ValueError("agent_core_command_shape_invalid")
    stop_after_phase = value.get("stopAfterPhase")
    if stop_after_phase is not None and stop_after_phase not in {
        "phase02",
        "phase03",
        "phase04",
        "phase05",
    }:
        raise ValueError("agent_core_stop_phase_invalid")
    return value


def main(argv: Optional[list[str]] = None) -> int:
    resolved_argv = sys.argv[1:] if argv is None else argv
    if resolved_argv:
        raise ValueError("agent_core_cli_arguments_forbidden")
    command = _agent_core_command(
        sys.stdin.buffer.read(AGENT_CORE_COMMAND_MAX_BYTES + 1)
    )
    clarification = command.get("clarification")
    intent_revision_context = command.get("intentRevisionContext")
    topic_selection = _parse_external_topic_selection(
        json.dumps(command["topicSelection"])
        if command.get("topicSelection") is not None
        else None
    )
    topic_choice_answer = _parse_external_topic_choice_answer(
        json.dumps(command["topicChoiceAnswer"])
        if command.get("topicChoiceAnswer") is not None
        else None
    )
    if topic_selection is not None and topic_choice_answer is not None:
        raise ValueError("agent_core_topic_inputs_conflict")
    raw_dispatch = command.get("runDispatch")
    run_dispatch = None
    if raw_dispatch is not None:
        if (
            not isinstance(raw_dispatch, Mapping)
            or set(raw_dispatch) != {"dispatchId", "ownerId", "leaseEpoch"}
            or not isinstance(raw_dispatch.get("dispatchId"), str)
            or not str(raw_dispatch["dispatchId"]).strip()
            or not isinstance(raw_dispatch.get("ownerId"), str)
            or not str(raw_dispatch["ownerId"]).strip()
            or isinstance(raw_dispatch.get("leaseEpoch"), bool)
            or not isinstance(raw_dispatch.get("leaseEpoch"), int)
            or int(raw_dispatch["leaseEpoch"]) < 1
        ):
            raise ValueError("agent_core_dispatch_invalid")
        run_dispatch = {
            "dispatch_id": raw_dispatch["dispatchId"],
            "dispatch_owner_id": raw_dispatch["ownerId"],
            "lease_epoch": raw_dispatch["leaseEpoch"],
        }

    core = ConversationAgentCore.from_environment()
    admission_lease = None
    try:
        admission_lease = PostgresAgentRuntimeAdmissionLease.acquire(
            connection=core.store.connection,
            actor_id=str(command["userId"]),
            environ=os.environ,
        )
        result = core.run_message(
            thread_id=str(command["threadId"]),
            run_id=str(command["runId"]),
            user_message=str(command["message"]),
            user_id=str(command["userId"]),
            artifact_root=str(command.get("artifactRoot") or "artifacts/phase-7"),
            clarification=clarification,
            run_dispatch=run_dispatch,
            analysis_context=(
                {"as_of": command["asOf"]} if command.get("asOf") else None
            ),
            intent_revision_context=intent_revision_context,
            topic_selection=topic_selection,
            topic_choice_answer=topic_choice_answer,
            stop_after_phase=command.get("stopAfterPhase"),
        )
    finally:
        try:
            if admission_lease is not None:
                admission_lease.release()
        finally:
            core.store.connection.close()
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return (
        0
        if result["status"]
        in {
            "planned",
            "evidence_ready",
            "authority_sealed",
            "narrative_ready",
            "completed",
            "interaction_completed",
            "waiting_for_clarification",
            "decision_recorded",
            "material_revision_required",
            "run_cancelled",
            "challenge_recorded",
        }
        else 1
    )


def _parse_external_topic_selection(
    raw: str | None,
) -> dict[str, str] | None:
    if raw is None:
        return None
    value = json.loads(raw)
    if not isinstance(value, Mapping) or set(value) != {
        "sourceRunId",
        "topicId",
    }:
        raise ValueError("topic_selection_envelope_invalid")
    source_run_id = value.get("sourceRunId")
    topic_id = value.get("topicId")
    if (
        not isinstance(source_run_id, str)
        or not source_run_id.strip()
        or source_run_id != source_run_id.strip()
        or not isinstance(topic_id, str)
        or not topic_id.strip()
        or topic_id != topic_id.strip()
    ):
        raise ValueError("topic_selection_envelope_invalid")
    return {"source_run_id": source_run_id, "topic_id": topic_id}


def _parse_external_topic_choice_answer(
    raw: str | None,
) -> dict[str, str] | None:
    if raw is None:
        return None
    value = json.loads(raw)
    if not isinstance(value, Mapping) or set(value) != {
        "sourceRunId",
        "answer",
    }:
        raise ValueError("topic_choice_answer_envelope_invalid")
    source_run_id = value.get("sourceRunId")
    answer = value.get("answer")
    if (
        not isinstance(source_run_id, str)
        or not source_run_id.strip()
        or source_run_id != source_run_id.strip()
        or not isinstance(answer, str)
        or not answer.strip()
        or answer != answer.strip()
    ):
        raise ValueError("topic_choice_answer_envelope_invalid")
    return {"source_run_id": source_run_id, "answer": answer}


if __name__ == "__main__":
    raise SystemExit(main())
