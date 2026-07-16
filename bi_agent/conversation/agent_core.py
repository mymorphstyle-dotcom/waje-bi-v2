from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Callable, Optional
from uuid import uuid4

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.conversation.clarification_authority import (
    build_material_authority,
    preflight_completed_material_authority,
)
from bi_agent.conversation.clarification_options import (
    clarification_labels_match,
    project_clarification_recommendation,
)
from bi_agent.conversation.models import (
    ClarificationOption,
    ClarificationState,
    sign_result_reuse_candidate,
)
from bi_agent.conversation.runtime import (
    ConversationOrchestrationError,
    ConversationRuntime,
)
from bi_agent.runtime.analysis_assets import build_analysis_assets
from bi_agent.runtime.answer_package import (
    reproject_answer_package_from_persisted_authority,
    reverify_answer_package_for_delivery,
)
from bi_agent.runtime.answer_package_artifact import (
    build_answer_package_artifact_record,
)
from bi_agent.runtime.analysis_contracts import (
    analysis_contract_from_dict,
    analysis_contract_signature,
)
from bi_agent.runtime.analysis_obligations import (
    ObligationRequest,
    resolve_analysis_obligations,
)
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)
from bi_agent.runtime.artifacts import synchronize_existing_artifact
from bi_agent.runtime.langgraph_workflow import run_pattern_workflow
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.runtime.permission_roles import resolve_product_runtime_roles


WorkflowRunner = Callable[[dict[str, Any]], Any]


class RunFailureFinalizationError(RuntimeError):
    code = "analysis_run_failure_finalization_unverified"

    def __init__(
        self,
        *,
        failure_reason: str,
        failure_stage: str,
        primary_error: Exception,
        persistence_error: Exception,
    ) -> None:
        super().__init__(self.code)
        self.failure_reason = failure_reason
        self.failure_stage = failure_stage
        self.primary_error = primary_error
        self.persistence_error = persistence_error


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


class ConversationAgentCore:
    def __init__(
        self,
        store: Any,
        *,
        workflow_runner: Optional[WorkflowRunner] = None,
        conversation_llm_client: Any = None,
        row_provider: Any = None,
        evidence_resolver: Any = None,
        rows_loader: Any = None,
        evidence_writer: Any = None,
        runtime_registry: RuntimeContractRegistry | None = None,
        release_resolver: Any = None,
        analysis_runtime: Any = None,
    ) -> None:
        self.store = store
        self.workflow_runner = workflow_runner or run_pattern_workflow
        self.conversation_llm_client = conversation_llm_client
        self.row_provider = row_provider
        self.evidence_resolver = evidence_resolver
        self.rows_loader = rows_loader
        self.evidence_writer = evidence_writer
        self.runtime_registry = runtime_registry
        self.release_resolver = release_resolver
        self.analysis_runtime = analysis_runtime

    def run_message(
        self,
        *,
        thread_id: str,
        run_id: str | None = None,
        user_message: str,
        user_id: str | None = None,
        permission_context: dict | None = None,
        role: str | None = None,
        runtime_permission_scope: str | None = None,
        artifact_root: str = "artifacts/phase-7",
        clarification: dict[str, Any] | None = None,
        clarification_dispatch: dict[str, str] | None = None,
        run_dispatch: dict[str, Any] | None = None,
        prior_analysis_assets: tuple[Mapping[str, Any], ...] = (),
        analysis_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        analysis_context = _validated_analysis_context(analysis_context)
        context_role = (permission_context or {}).get("role")
        role, runtime_permission_scope = resolve_product_runtime_roles(
            role,
            runtime_permission_scope,
            permission_context_role=(
                str(context_role) if context_role not in (None, "") else None
            ),
        )
        permission_context = {
            **{
                key: value
                for key, value in (permission_context or {}).items()
                if key not in {"role", "permission_scope", "runtime_permission_scope"}
            },
            "role": role,
        }
        run_id = run_id or f"run-{uuid4().hex[:12]}"
        thread = self.store.get_thread(thread_id)
        if run_dispatch:
            claim_dispatch = getattr(self.store, "claim_run_dispatch", None)
            if not callable(claim_dispatch):
                raise EvidenceIntegrityError(
                    "run_dispatch_claim_resolver_missing"
                )
            claim_dispatch(
                run_id=run_id,
                thread_id=thread_id,
                dispatch_owner_id=str(
                    run_dispatch.get("dispatch_owner_id") or ""
                ),
                lease_epoch=run_dispatch.get("lease_epoch"),
            )
        elif clarification_dispatch:
            claim_dispatch = getattr(
                self.store,
                "claim_clarification_dispatch",
                None,
            )
            if not callable(claim_dispatch):
                raise EvidenceIntegrityError(
                    "clarification_dispatch_claim_resolver_missing"
                )
            claim_dispatch(
                source_run_id=str(
                    clarification_dispatch.get("source_run_id") or ""
                ),
                resumed_run_id=run_id,
                thread_id=thread_id,
                dispatch_owner_id=str(
                    clarification_dispatch.get("dispatch_owner_id") or ""
                ),
            )
        else:
            self.store.upsert_run(run_id, thread_id=thread_id, status="running")
        try:
            _emit_agent_core_startup_ack()
            clarification_resume_claim: dict[str, Any] = {}
            if clarification and clarification.get("runId"):
                resolve_resume_claim = getattr(
                    self.store,
                    "resolve_clarification_resume_claim",
                    None,
                )
                if not callable(resolve_resume_claim):
                    raise EvidenceIntegrityError(
                        "clarification_resume_claim_resolver_missing"
                    )
                clarification_resume_claim = dict(
                    resolve_resume_claim(
                        source_run_id=str(clarification["runId"]),
                        resumed_run_id=run_id,
                        thread_id=thread_id,
                        answer=str(
                            clarification.get("answer") or user_message
                        ).strip(),
                        selected_option_id=(
                            str(clarification["selectedOptionId"])
                            if clarification.get("selectedOptionId") is not None
                            else None
                        ),
                        source=str(clarification.get("source") or "user"),
                    )
                )
            if clarification:
                self.store.add_audit_event(
                    "clarification_answer_submitted",
                    thread_id=thread_id,
                    run_id=run_id,
                    ref=run_id,
                    payload=clarification,
                )
            turn = ConversationRuntime(
                self.store,
                llm_client=self.conversation_llm_client,
            ).handle_message(
                thread_id,
                user_message,
                role=role,
                run_id=run_id,
                prior_analysis_assets=tuple(prior_analysis_assets or ()),
                analysis_context=analysis_context,
                clarification_resume_claim=clarification_resume_claim,
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
            if turn.needs_clarification:
                clarification_payload = (
                    turn.clarification.to_dict() if turn.clarification else None
                )
                request = {
                    "reason": "needs_clarification",
                    "intent": turn.turn_intent.intent,
                    "clarification": clarification_payload,
                    "clarification_answer": clarification,
                    "user_id": user_id,
                    "permission_context": permission_context,
                    "clarification_source_envelope": (
                        _build_clarification_source_envelope(
                            source_run_id=run_id,
                            source_thread_id=thread_id,
                            source_topic_id=turn.topic_id or "",
                            source_owner_id=thread.owner_id,
                            question=user_message,
                            analysis_context=analysis_context,
                            clarification=clarification_payload or {},
                        )
                    ),
                }
                self.store.upsert_run(
                    run_id,
                    thread_id=thread_id,
                    turn_id=turn.turn_id,
                    topic_id=turn.topic_id or "",
                    status="waiting_for_clarification",
                    request=request,
                )
                self.store.add_audit_event(
                    "clarification_requested",
                    thread_id=thread_id,
                    topic_id=turn.topic_id or "",
                    run_id=run_id,
                    ref=run_id,
                    payload=request["clarification"] or {},
                )
                return {
                    "status": "waiting_for_clarification",
                    "run_id": run_id,
                    "turn_id": turn.turn_id,
                    "topic_id": turn.topic_id,
                    "intent": turn.turn_intent.intent,
                    "topic_relation": turn.topic_relation,
                    "clarification": request["clarification"],
                    "context_manifest": context_manifest,
                }
            self.store.upsert_run(
                run_id,
                thread_id=thread_id,
                turn_id=turn.turn_id,
                topic_id=turn.topic_id or "",
                status="completed_without_workflow",
                request={"reason": turn.turn_intent.intent},
            )
            return {
                "status": "completed_without_workflow",
                "run_id": run_id,
                "turn_id": turn.turn_id,
                "topic_id": turn.topic_id,
                "intent": turn.turn_intent.intent,
                "topic_relation": turn.topic_relation,
                "context_manifest": context_manifest,
            }

        request = turn.run_request.to_dict()
        resume_context = request.get("clarification_resume_context") or {}
        clarification_choice = _clarification_choice_from_answer(
            user_message,
            turn.turn_intent.intent,
            explicit_choice=clarification,
            selected_material_action=(
                resume_context.get("selected_material_action") or {}
            ),
        )
        request["context_manifest"] = context_manifest
        request["reuse_decisions"] = [decision.to_dict() for decision in turn.reuse_decisions]
        original_question = ""
        if resume_context:
            raw_original_question = resume_context.get("question")
            if (
                not isinstance(raw_original_question, str)
                or not raw_original_question
                or raw_original_question != raw_original_question.strip()
            ):
                raise ConversationOrchestrationError(
                    "clarification_source_envelope_invalid"
                )
            original_question = raw_original_question
        request.update(
            {
                "run_id": run_id,
                "question": original_question if resume_context else user_message,
                "clarification_user_message": (
                    user_message if resume_context else ""
                ),
                "role": role,
                "runtime_permission_scope": runtime_permission_scope,
                "user_id": user_id,
                "permission_context": permission_context,
                "artifact_root": artifact_root,
                "clarification_answer": clarification,
                "prior_analysis_assets": tuple(turn.run_request.prior_analysis_assets or ()),
                "analysis_context": dict(turn.run_request.analysis_context or {}),
            }
        )
        if clarification_choice:
            request["clarification_choice"] = clarification_choice
        accepted_degradation_choice = dict(
            resume_context.get("accepted_degradation_choice") or {}
        )
        if not accepted_degradation_choice:
            accepted_degradation_choice = dict(
                next(
                    (
                        item
                        for item in context_manifest.get("accepted_assumptions") or ()
                        if isinstance(item, Mapping)
                    ),
                    {},
                )
            )
        if accepted_degradation_choice:
            action_kind = str(
                accepted_degradation_choice.get("action_kind") or ""
            )
            if action_kind in {
                "omit_unavailable_context",
                "continue_with_boundary_only",
            }:
                accepted_degradation_choice = (
                    _authority_closed_degradation_choice(
                        accepted_degradation_choice,
                        {
                            "analysis_contract": resume_context.get(
                                "analysis_contract"
                            )
                        },
                        self.runtime_registry,
                    )
                )
                resume_context = {
                    **dict(resume_context),
                    "accepted_degradation_choice": (
                        accepted_degradation_choice
                    ),
                }
                request["clarification_resume_context"] = resume_context
            request["accepted_degradation_choice"] = accepted_degradation_choice
            if action_kind in {
                "omit_unavailable_context",
                "continue_with_boundary_only",
            }:
                source_run_id = str(resume_context.get("resume_run_id") or "")
                record_outcome = getattr(
                    self.store, "record_clarification_outcome", None
                )
                resolve_authority = getattr(
                    self.store, "resolve_clarification_resume_authority", None
                )
                try:
                    if (
                        not source_run_id
                        or not callable(record_outcome)
                        or not callable(resolve_authority)
                    ):
                        raise ValueError(
                            "clarification_resume_authority_unavailable"
                        )
                    outcome_ref = record_outcome(
                        source_run_id=source_run_id,
                        thread_id=thread_id,
                        topic_id=turn.topic_id or "",
                        choice=accepted_degradation_choice,
                    )
                    authority = resolve_authority(
                        source_run_id=source_run_id,
                        thread_id=thread_id,
                        topic_id=turn.topic_id or "",
                        choice=accepted_degradation_choice,
                        outcome_ref=outcome_ref,
                    )
                except Exception as exc:
                    self.store.upsert_run(
                        run_id,
                        thread_id=thread_id,
                        turn_id=turn.turn_id,
                        topic_id=turn.topic_id or "",
                        status="failed",
                        request={
                            **_persistable_request(request),
                            "failure_reason": (
                                "clarification_resume_authority_failed"
                            ),
                        },
                    )
                    self.store.add_audit_event(
                        "clarification_resume_authority_failed",
                        thread_id=thread_id,
                        topic_id=turn.topic_id or "",
                        run_id=run_id,
                        ref=source_run_id,
                        payload={
                            "reason": str(exc),
                            "error_type": type(exc).__name__,
                        },
                    )
                    return {
                        "status": "failed",
                        "run_id": run_id,
                        "turn_id": turn.turn_id,
                        "topic_id": turn.topic_id,
                        "failure_reason": (
                            "clarification_resume_authority_failed"
                        ),
                    }
                request["accepted_degradation_choice"] = accepted_degradation_choice
                context_manifest = _manifest_with_accepted_choice(
                    context_manifest,
                    accepted_degradation_choice,
                )
                request["context_manifest"] = context_manifest
                self.store.record_context_manifest(context_manifest)
                request["accepted_terminal_gap_authority"] = authority
                request["clarification_outcome_ref"] = outcome_ref
        if self.row_provider is not None:
            request["row_provider"] = self.row_provider
        if self.evidence_resolver is not None:
            request["evidence_resolver"] = self.evidence_resolver
        if self.rows_loader is not None:
            request["rows_loader"] = self.rows_loader
        if self.evidence_writer is not None:
            request["evidence_writer"] = self.evidence_writer
        if self.runtime_registry is not None:
            request["runtime_registry"] = self.runtime_registry
        if self.release_resolver is not None:
            request["release_resolver"] = self.release_resolver
        if self.analysis_runtime is not None:
            request["analysis_runtime"] = self.analysis_runtime
            request["run_mode"] = "production"
        selected_action = resume_context.get("selected_query_gap_action") or {}
        action_kind = str(selected_action.get("action_kind") or "")
        if action_kind in {"wait_for_source", "user_redirect"}:
            prior_clarification = dict(resume_context.get("clarification") or {})
            clarification_source_envelope = _build_clarification_source_envelope(
                source_run_id=run_id,
                source_thread_id=thread_id,
                source_topic_id=turn.topic_id or "",
                source_owner_id=thread.owner_id,
                question=str(request.get("question") or ""),
                analysis_context=request.get("analysis_context") or {},
                accepted_graph=resume_context.get("accepted_graph") or (),
                analysis_contract=resume_context.get("analysis_contract") or {},
                analysis_route=resume_context.get("analysis_route") or {},
                original_intent=resume_context.get("original_intent") or {},
                material_slots=resume_context.get("material_slots") or {},
                clarification=prior_clarification,
            )
            if action_kind == "wait_for_source":
                questions = tuple(prior_clarification.get("questions") or ())
                first_question = next(
                    (item for item in questions if isinstance(item, Mapping)),
                    {},
                )
                raw_recommended = prior_clarification.get("recommended_assumption") or {}
                recommended = str(
                    raw_recommended.get("option")
                    if isinstance(raw_recommended, Mapping)
                    else raw_recommended
                ).strip()
                options = [
                    ClarificationOption(
                        option_id=_clarification_option_id(
                            prior_clarification,
                            str(label),
                            index,
                        ),
                        label=str(label),
                        description=str(label),
                        recommended=str(label) == recommended,
                    )
                    for index, label in enumerate(first_question.get("options") or ())
                    if str(label)
                ]
                self.store.set_pending_clarification(
                    thread_id,
                    turn.topic_id or "",
                    run_id,
                )
                self.store.save_clarification_state(
                    ClarificationState(
                        run_id=run_id,
                        topic_id=turn.topic_id or "",
                        question=str(first_question.get("question") or ""),
                        options=options,
                    )
                )
            else:
                self.store.clear_pending_clarification(thread_id)
            self.store.upsert_run(
                run_id,
                thread_id=thread_id,
                turn_id=turn.turn_id,
                topic_id=turn.topic_id or "",
                status="waiting_for_clarification",
                request={
                    **_persistable_request(request),
                    "clarification": prior_clarification,
                    "selected_query_gap_action": dict(selected_action),
                    "clarification_source_envelope": (
                        clarification_source_envelope
                    ),
                },
            )
            return {
                "status": "waiting_for_clarification",
                "run_id": run_id,
                "turn_id": turn.turn_id,
                "topic_id": turn.topic_id,
                "intent": turn.turn_intent.intent,
                "topic_relation": turn.topic_relation,
                "context_manifest": context_manifest,
                "clarification": prior_clarification,
                "selected_query_gap_action": dict(selected_action),
                "user_redirect": action_kind == "user_redirect",
            }
        self.store.upsert_run(
            run_id,
            thread_id=thread_id,
            turn_id=turn.turn_id,
            topic_id=turn.topic_id or "",
            status="running_workflow",
            request=_persistable_request(request),
        )
        result = self.workflow_runner(request)
        workflow_llm_calls = tuple(
            dict(call)
            for call in result.llm_calls
            if isinstance(call, Mapping)
        )
        self.store.record_run_nodes(run_id, tuple(result.checkpoint_events))
        if result.status == "waiting_for_clarification" and result.answer_package:
            publication_stage = "material_authority_projection"
            try:
                source_route = result.answer_package.get("analysis_route") or {}
                if not isinstance(source_route, Mapping):
                    raise ValueError("analysis_route_invalid")
                obligation_resolution = (
                    source_route.get("obligation_resolution") or {}
                )
                if not isinstance(obligation_resolution, Mapping):
                    raise ValueError("obligation_resolution_invalid")
                material_authority = build_material_authority(
                    source_run_id=run_id,
                    thread_id=thread_id,
                    topic_id=turn.topic_id or "",
                    original_intent=(
                        result.answer_package.get("original_intent") or {}
                    ),
                    material_slots=(
                        result.answer_package.get("material_slots") or {}
                    ),
                    runtime_material=result.answer_package.get(
                        "execution_material"
                    ),
                    obligation_rejection_history=(
                        obligation_resolution.get("mutation_history") or ()
                    ),
                )
                publication_stage = "runtime_bundle_validation"
                if result.analysis_runtime_records is None:
                    if result.answer_package.get("analysis_contract"):
                        raise ValueError("analysis_runtime_records_missing")
                else:
                    records = dict(result.analysis_runtime_records)
                    runtime_result = getattr(result, "analysis_runtime_result", None)
                    partial_publication_audit: dict[str, Any] = {}
                    if self.analysis_runtime is not None and runtime_result is not None:
                        records = self.analysis_runtime.build_persistence_bundle(
                            runtime_result,
                            answer_package=result.answer_package,
                            request=request,
                            artifact_path=result.artifact_path,
                            publication_mode="waiting_for_clarification",
                            publication_audit=partial_publication_audit,
                        )
                    if records.get("trusted_provenance_records"):
                        publication_stage = "artifact_synchronization"
                        if not result.artifact_path or not synchronize_existing_artifact(
                            result.answer_package,
                            result.artifact_path,
                        ):
                            raise EvidenceIntegrityError(
                                "analysis_runtime_artifact_sync_failed"
                            )
                        records["answer_package_artifacts"] = (
                            build_answer_package_artifact_record(
                                run_id=run_id,
                                artifact_path=result.artifact_path,
                                answer_package=result.answer_package,
                            ),
                        )
                    publication_stage = "runtime_bundle_validation"
                    _preflight_analysis_runtime_bundle(
                        store=self.store,
                        run_id=run_id,
                        records=records,
                    )
                    publication_stage = "store_commit"
                    self.store.save_analysis_runtime_records(run_id=run_id, **records)
                    if partial_publication_audit.get("omitted_result_count"):
                        self.store.add_audit_event(
                            "analysis_runtime_partial_publication",
                            thread_id=thread_id,
                            topic_id=turn.topic_id or "",
                            run_id=run_id,
                            ref=run_id,
                            payload=partial_publication_audit,
                        )
            except Exception as exc:
                failure_reason = _analysis_runtime_failure_reason(
                    publication_stage
                )
                return _finalize_analysis_run_failure(
                    store=self.store,
                    failure_reason=failure_reason,
                    failure_stage=publication_stage,
                    exc=exc,
                    run_id=run_id,
                    thread_id=thread_id,
                    turn_id=turn.turn_id,
                    topic_id=turn.topic_id or "",
                    request=request,
                    artifact_path=result.artifact_path,
                    context_manifest=context_manifest,
                    intent=turn.turn_intent.intent,
                    topic_relation=turn.topic_relation,
                    llm_calls=workflow_llm_calls,
                )
            clarification_payload = dict(
                result.answer_package.get("clarification") or {}
            )
            raw_recommended = clarification_payload.get("recommended_assumption")
            recommended_text = str(
                (
                    raw_recommended.get("option")
                    or raw_recommended.get("assumption")
                    or ""
                )
                if isinstance(raw_recommended, Mapping)
                else raw_recommended or ""
            ).strip()
            recommended_choice_id = str(
                clarification_payload.get("recommended_choice_id") or ""
            )
            if not recommended_choice_id:
                recommended_choice_id = next(
                    (
                        str(action.get("choice_id") or "")
                        for action in clarification_payload.get("choice_actions") or ()
                        if isinstance(action, Mapping)
                        and clarification_labels_match(
                            action.get("business_label")
                            or action.get("business_semantics"),
                            recommended_text,
                        )
                    ),
                    "",
                )
            progressing_action = next(
                (
                    dict(action)
                    for action in clarification_payload.get("choice_actions") or ()
                    if isinstance(action, Mapping)
                    and str(action.get("action_kind") or "")
                    not in {"wait_for_source", "user_redirect"}
                ),
                {},
            )
            recommended_action = next(
                (
                    action
                    for action in clarification_payload.get("choice_actions") or ()
                    if isinstance(action, Mapping)
                    and str(action.get("choice_id") or "")
                    == recommended_choice_id
                ),
                {},
            )
            if (
                progressing_action
                and str(recommended_action.get("action_kind") or "")
                == "wait_for_source"
            ):
                recommended_choice_id = str(
                    progressing_action.get("choice_id") or ""
                )
            clarification_payload = project_clarification_recommendation(
                clarification_payload,
                recommended_choice_id=recommended_choice_id,
            )
            recommended_choice_id = str(
                clarification_payload.get("recommended_choice_id") or ""
            )
            questions = tuple(clarification_payload.get("questions") or ())
            first_question = next(
                (item for item in questions if isinstance(item, Mapping)),
                {},
            )
            option_labels = tuple(
                str(item)
                for item in first_question.get("options") or ()
                if str(item)
            )
            options = [
                ClarificationOption(
                    option_id=(
                        option_id := _clarification_option_id(
                            clarification_payload,
                            label,
                            index,
                        )
                    ),
                    label=label,
                    description=label,
                    recommended=option_id == recommended_choice_id,
                )
                for index, label in enumerate(option_labels)
            ]
            clarification_state = ClarificationState(
                run_id=run_id,
                topic_id=turn.topic_id or "",
                question=str(first_question.get("question") or ""),
                options=options,
            )
            clarification_source_envelope = _build_clarification_source_envelope(
                source_run_id=run_id,
                source_thread_id=thread_id,
                source_topic_id=turn.topic_id or "",
                source_owner_id=thread.owner_id,
                question=str(request.get("question") or ""),
                analysis_context=request.get("analysis_context") or {},
                accepted_graph=result.answer_package.get("accepted_graph") or (),
                analysis_contract=(
                    result.answer_package.get("analysis_contract") or {}
                ),
                analysis_route=result.answer_package.get("analysis_route") or {},
                original_intent=result.answer_package.get("original_intent") or {},
                material_slots=result.answer_package.get("material_slots") or {},
                clarification=clarification_payload,
            )
            waiting_request = {
                **_persistable_request(request),
                "accepted_graph": list(
                    result.answer_package.get("accepted_graph") or ()
                ),
                "analysis_contract": dict(
                    result.answer_package.get("analysis_contract") or {}
                ),
                "analysis_route": dict(
                    result.answer_package.get("analysis_route") or {}
                ),
                "original_intent": dict(
                    result.answer_package.get("original_intent") or {}
                ),
                "material_slots": dict(
                    result.answer_package.get("material_slots") or {}
                ),
                "material_authority": material_authority,
                "clarification": clarification_payload,
                "clarification_source_envelope": clarification_source_envelope,
            }
            try:
                self.store.finalize_waiting_clarification(
                    run_id=run_id,
                    thread_id=thread_id,
                    turn_id=turn.turn_id,
                    topic_id=turn.topic_id or "",
                    request=waiting_request,
                    clarification_state=clarification_state,
                )
            except Exception as exc:
                return _finalize_analysis_run_failure(
                    store=self.store,
                    failure_reason="analysis_runtime_store_commit_failed",
                    failure_stage="store_commit",
                    exc=exc,
                    run_id=run_id,
                    thread_id=thread_id,
                    turn_id=turn.turn_id,
                    topic_id=turn.topic_id or "",
                    request=request,
                    artifact_path=result.artifact_path,
                    context_manifest=context_manifest,
                    intent=turn.turn_intent.intent,
                    topic_relation=turn.topic_relation,
                    llm_calls=workflow_llm_calls,
                )
            return {
                "status": "waiting_for_clarification",
                "run_id": run_id,
                "turn_id": turn.turn_id,
                "topic_id": turn.topic_id,
                "intent": turn.turn_intent.intent,
                "topic_relation": turn.topic_relation,
                "context_manifest": context_manifest,
                "clarification": clarification_payload,
                "accepted_graph": list(
                    result.answer_package.get("accepted_graph") or ()
                ),
                "analysis_contract": dict(
                    result.answer_package.get("analysis_contract") or {}
                ),
                "analysis_route": dict(
                    result.answer_package.get("analysis_route") or {}
                ),
                "original_intent": dict(
                    result.answer_package.get("original_intent") or {}
                ),
                "material_slots": dict(
                    result.answer_package.get("material_slots") or {}
                ),
                "artifact_path": result.artifact_path,
                "llm_calls": list(workflow_llm_calls),
            }
        if result.status != "draft" or not result.answer_package:
            failure_owner = (
                "evidence_verifier_owner"
                if str(result.failure_reason).startswith("delivery_reverify_failed")
                else "workflow_runtime_owner"
            )
            _record_workflow_failure_llm_audits(
                self.store,
                thread_id=thread_id,
                topic_id=turn.topic_id or "",
                run_id=run_id,
                llm_calls=workflow_llm_calls,
            )
            self.store.upsert_run(
                run_id,
                thread_id=thread_id,
                turn_id=turn.turn_id,
                topic_id=turn.topic_id or "",
                status="failed",
                request={
                    **_persistable_request(request),
                    "failure_reason": result.failure_reason,
                    "failure_owner": failure_owner,
                },
            )
            self.store.add_audit_event(
                "workflow_failed",
                thread_id=thread_id,
                topic_id=turn.topic_id or "",
                run_id=run_id,
                payload={
                    "failure_reason": result.failure_reason,
                    "failure_owner": failure_owner,
                },
            )
            return {
                "status": "failed",
                "run_id": run_id,
                "turn_id": turn.turn_id,
                "topic_id": turn.topic_id,
                "intent": turn.turn_intent.intent,
                "topic_relation": turn.topic_relation,
                "context_manifest": context_manifest,
                "failure_reason": result.failure_reason,
                "failure_owner": failure_owner,
                "answer_package": result.answer_package,
                "artifact_path": result.artifact_path,
                "llm_calls": list(workflow_llm_calls),
            }

        authority_package = dict(canonical_value(result.answer_package))
        internal_verifier_audit: dict[str, Any] = {}
        package = reverify_answer_package_for_delivery(
            result.answer_package,
            evidence_resolver=self.evidence_resolver,
            rows_loader=self.rows_loader,
            runtime_registry=self.runtime_registry,
            release_resolver=self.release_resolver,
            internal_verifier_audit=internal_verifier_audit,
        )
        self.store.add_audit_event(
            "delivery_verifier_completed",
            thread_id=thread_id,
            topic_id=turn.topic_id or "",
            run_id=run_id,
            ref=run_id,
            payload=internal_verifier_audit,
        )
        package["run_id"] = run_id
        package["artifact_path"] = result.artifact_path
        authority_package["run_id"] = run_id
        authority_package["artifact_path"] = result.artifact_path
        authority_admin = dict(authority_package.get("admin_audit") or {})
        authority_admin["verifier"] = canonical_value(internal_verifier_audit)
        authority_package["admin_audit"] = authority_admin
        authority_sections = list(authority_package.get("sections") or ())
        if not any(
            isinstance(section, Mapping)
            and str(section.get("section_id") or section.get("id") or "")
            == "admin_audit"
            for section in authority_sections
        ):
            authority_sections.append(
                {
                    "id": "admin_audit",
                    "visibility": "admin_audit",
                    "payload": dict(authority_admin),
                }
            )
            authority_package["sections"] = authority_sections
        verifier_status = str(
            package.get("admin_audit", {}).get("verifier", {}).get("status") or ""
        )
        narrative_publication_failed = (
            bool(package.get("narrative_publication_block"))
            or str(
                package.get("admin_audit", {})
                .get("narrative_publication", {})
                .get("status")
                or ""
            )
            == "failed"
        )
        delivery_failure_reason = (
            "narrative_publication_failed"
            if narrative_publication_failed
            else "delivery_verifier_failed"
        )
        delivery_failed = (
            str(package.get("status") or "") == "failed"
            or verifier_status == "failed"
            or bool(package.get("evidence_verifier_block"))
            or bool(package.get("quality_gate", {}).get("blocks_display"))
        )
        if delivery_failed:
            _record_workflow_failure_llm_audits(
                self.store,
                thread_id=thread_id,
                topic_id=turn.topic_id or "",
                run_id=run_id,
                llm_calls=workflow_llm_calls,
            )
            self.store.upsert_run(
                run_id,
                thread_id=thread_id,
                turn_id=turn.turn_id,
                topic_id=turn.topic_id or "",
                status="failed",
                request={
                    **_persistable_request(request),
                    "failure_reason": delivery_failure_reason,
                },
            )
            self.store.add_audit_event(
                "answer_publication_blocked",
                thread_id=thread_id,
                topic_id=turn.topic_id or "",
                run_id=run_id,
                ref=run_id,
                payload={"reason": delivery_failure_reason},
            )
            return {
                "status": "failed",
                "run_id": run_id,
                "turn_id": turn.turn_id,
                "topic_id": turn.topic_id,
                "intent": turn.turn_intent.intent,
                "topic_relation": turn.topic_relation,
                "artifact_path": result.artifact_path,
                "answer_package": package,
                "context_manifest": context_manifest,
                "failure_reason": delivery_failure_reason,
                "llm_calls": list(workflow_llm_calls),
                "quality_review": package.get("quality_gate")
                or package.get("admin_audit"),
            }
        accepted_graph = (
            package.get("accepted_graph")
            or package.get("admin_audit", {}).get("accepted_graph")
            or []
        )
        summary_claims = tuple(
            claim
            for section in package.get("sections") or ()
            if isinstance(section, Mapping)
            and (section.get("section_id") or section.get("id")) == "summary"
            for claim in (section.get("payload") or {}).get("claims") or ()
            if isinstance(claim, Mapping)
        )
        authority_evidence = tuple(
            evidence
            for section in package.get("sections") or ()
            if isinstance(section, Mapping)
            for evidence in (section.get("payload") or {}).get("evidence") or ()
            if isinstance(evidence, Mapping)
            and evidence.get("binding_manifest_ref")
        )
        persisted_context_manifest = None
        records: dict[str, Any] | None = None
        analysis_assets: tuple[dict[str, Any], ...] = ()
        result_candidate_records: Mapping[str, Any] | None = None
        completed_material_authority = getattr(
            result,
            "completed_material_authority",
            None,
        )
        publication_stage = "material_authority_projection"
        try:
            if (
                (
                    getattr(result, "analysis_runtime_result", None)
                    is not None
                    or result.analysis_runtime_records is not None
                )
                and not isinstance(completed_material_authority, Mapping)
            ):
                raise ValueError(
                    "completed_material_authority_missing_or_invalid"
                )
            publication_stage = "runtime_bundle_validation"
            if result.analysis_runtime_records is None:
                if summary_claims or authority_evidence:
                    raise ValueError("analysis_runtime_records_missing")
            else:
                records = dict(result.analysis_runtime_records)
                runtime_result = getattr(result, "analysis_runtime_result", None)
                if self.analysis_runtime is not None and runtime_result is not None:
                    records = self.analysis_runtime.build_persistence_bundle(
                        runtime_result,
                        answer_package=authority_package,
                        request=request,
                        artifact_path=result.artifact_path,
                    )
                try:
                    preflight_completed_material_authority(
                        material_authority=completed_material_authority,
                        analysis_contract=records.get("analysis_contract") or {},
                        run_id=run_id,
                        thread_id=thread_id,
                        topic_id=turn.topic_id or "",
                        runtime_registry=self.runtime_registry,
                    )
                except Exception as exc:
                    return _finalize_analysis_run_failure(
                        store=self.store,
                        failure_reason=(
                            "completed_material_authority_preflight_failed"
                        ),
                        exc=exc,
                        run_id=run_id,
                        thread_id=thread_id,
                        turn_id=turn.turn_id,
                        topic_id=turn.topic_id or "",
                        request=request,
                        artifact_path=result.artifact_path,
                        context_manifest=context_manifest,
                        intent=turn.turn_intent.intent,
                        topic_relation=turn.topic_relation,
                        llm_calls=workflow_llm_calls,
                    )
                verified_claims = tuple(records.get("verified_claims") or ())
                contexts_by_ref = {
                    str(item.get("manifest_id") or ""): item
                    for item in records.get("context_manifests") or ()
                    if isinstance(item, Mapping) and item.get("manifest_id")
                }
                if summary_claims:
                    context_refs = {
                        str(item.get("context_manifest_ref") or "")
                        for item in verified_claims
                        if isinstance(item, Mapping)
                    }
                    if (
                        not verified_claims
                        or len(context_refs) != 1
                        or not context_refs.issubset(contexts_by_ref)
                    ):
                        raise ValueError(
                            "analysis_runtime_verified_claim_context_missing"
                        )
                    persisted_context_manifest = dict(
                        contexts_by_ref[next(iter(context_refs))]
                    )
                package = reproject_answer_package_from_persisted_authority(
                    package,
                    persistence_records=records,
                )
                delivery_summary = next(
                    (
                        dict(section.get("payload") or {})
                        for section in package.get("sections") or ()
                        if isinstance(section, Mapping)
                        and str(
                            section.get("section_id")
                            or section.get("id")
                            or ""
                        )
                        == "summary"
                    ),
                    {},
                )
                for section in authority_package.get("sections") or ():
                    if not isinstance(section, dict) or str(
                        section.get("section_id")
                        or section.get("id")
                        or ""
                    ) != "summary":
                        continue
                    section["payload"] = {
                        **dict(section.get("payload") or {}),
                        **delivery_summary,
                    }
                for field in (
                    "status",
                    "context_manifest_ref",
                    "context_assumptions",
                    "accepted_degradation_choice",
                    "accepted_graph_metadata",
                    "final_answer",
                    "final_explanation",
                    "delivery_claim_ids",
                    "delivery_evidence_refs",
                ):
                    if field in package:
                        authority_package[field] = canonical_value(package[field])
                authority_package = (
                    reproject_answer_package_from_persisted_authority(
                        authority_package,
                        persistence_records=records,
                    )
                )
                publication_stage = "artifact_synchronization"
                if result.artifact_path and not synchronize_existing_artifact(
                    authority_package,
                    result.artifact_path,
                ):
                    raise EvidenceIntegrityError(
                        "analysis_runtime_artifact_sync_failed"
                    )
                if result.artifact_path:
                    records["answer_package_artifacts"] = (
                        build_answer_package_artifact_record(
                            run_id=run_id,
                            artifact_path=result.artifact_path,
                            answer_package=authority_package,
                        ),
                    )

            publication_stage = "runtime_bundle_validation"
            context_manifest = (
                persisted_context_manifest
                or _manifest_with_current_run_evidence(
                    context_manifest,
                    package,
                    role,
                )
            )
            if turn.topic_id and hasattr(self.store, "save_analysis_assets"):
                analysis_assets = build_analysis_assets(
                    package,
                    evidence_resolver=self.evidence_resolver,
                    rows_loader=self.rows_loader,
                    runtime_registry=self.runtime_registry,
                    release_resolver=self.release_resolver,
                )
            if records is not None:
                _preflight_analysis_runtime_bundle(
                    store=self.store,
                    run_id=run_id,
                    records=records,
                )
                publication_stage = "store_commit"
                self.store.save_analysis_runtime_records(run_id=run_id, **records)
                result_candidate_records = records
        except Exception as exc:
            failure_reason = _analysis_runtime_failure_reason(
                publication_stage
            )
            return _finalize_analysis_run_failure(
                store=self.store,
                failure_reason=failure_reason,
                failure_stage=publication_stage,
                exc=exc,
                run_id=run_id,
                thread_id=thread_id,
                turn_id=turn.turn_id,
                topic_id=turn.topic_id or "",
                request=request,
                artifact_path=result.artifact_path,
                context_manifest=context_manifest,
                intent=turn.turn_intent.intent,
                topic_relation=turn.topic_relation,
                llm_calls=workflow_llm_calls,
            )
        delivery_stage = "context_manifest"
        try:
            self.store.record_context_manifest(context_manifest)
            delivery_stage = "answer_package"
            self.store.record_answer_package(run_id, package)
            if turn.topic_id and hasattr(self.store, "save_analysis_assets"):
                delivery_stage = "analysis_assets"
                self.store.save_analysis_assets(
                    thread_id,
                    turn.topic_id,
                    analysis_assets,
                )
        except Exception as exc:
            try:
                self.store.recover_after_write_failure()
            except Exception as recovery_exc:
                exc.add_note(
                    "delivery write recovery failed: "
                    f"{type(recovery_exc).__name__}"
                )
            return _finalize_analysis_run_failure(
                store=self.store,
                failure_reason="analysis_delivery_persistence_failed",
                failure_stage=delivery_stage,
                exc=exc,
                run_id=run_id,
                thread_id=thread_id,
                turn_id=turn.turn_id,
                topic_id=turn.topic_id or "",
                request=request,
                artifact_path=result.artifact_path,
                context_manifest=context_manifest,
                intent=turn.turn_intent.intent,
                topic_relation=turn.topic_relation,
                llm_calls=workflow_llm_calls,
            )
        try:
            if isinstance(completed_material_authority, Mapping):
                self.store.finalize_completed_material_authority(
                    run_id=run_id,
                    thread_id=thread_id,
                    topic_id=turn.topic_id or "",
                    request=_persistable_request(request),
                    material_authority=completed_material_authority,
                )
            else:
                self.store.upsert_run(
                    run_id,
                    thread_id=thread_id,
                    turn_id=turn.turn_id,
                    topic_id=turn.topic_id or "",
                    status="completed",
                    request=_persistable_request(request),
                )
        except Exception as exc:
            if isinstance(completed_material_authority, Mapping):
                recovered_completion = _recover_completed_material_authority(
                    store=self.store,
                    run_id=run_id,
                    thread_id=thread_id,
                    turn_id=turn.turn_id,
                    topic_id=turn.topic_id or "",
                    expected_material_authority=completed_material_authority,
                )
            else:
                recovered_completion = _recover_generic_completion(
                    store=self.store,
                    run_id=run_id,
                    thread_id=thread_id,
                    turn_id=turn.turn_id,
                    topic_id=turn.topic_id or "",
                    expected_request=request,
                )
            if recovered_completion == "terminal_completed_conflict":
                return _terminal_completed_conflict_result(
                    store=self.store,
                    failure_reason=(
                        "completed_material_authority_finalization_failed"
                    ),
                    exc=exc,
                    run_id=run_id,
                    thread_id=thread_id,
                    turn_id=turn.turn_id,
                    topic_id=turn.topic_id or "",
                    artifact_path=result.artifact_path,
                    context_manifest=context_manifest,
                    intent=turn.turn_intent.intent,
                    topic_relation=turn.topic_relation,
                    llm_calls=workflow_llm_calls,
                )
            if recovered_completion != "completed_exact":
                return _finalize_analysis_run_failure(
                    store=self.store,
                    failure_reason=(
                        "completed_material_authority_finalization_failed"
                    ),
                    exc=exc,
                    run_id=run_id,
                    thread_id=thread_id,
                    turn_id=turn.turn_id,
                    topic_id=turn.topic_id or "",
                    request=request,
                    artifact_path=result.artifact_path,
                    context_manifest=context_manifest,
                    intent=turn.turn_intent.intent,
                    topic_relation=turn.topic_relation,
                    llm_calls=workflow_llm_calls,
                )
        followup_index_failure: dict[str, str] | None = None
        if result_candidate_records is not None:
            try:
                _publish_result_reuse_candidates(
                    self.store,
                    topic_id=turn.topic_id or "",
                    run_id=run_id,
                    request=request,
                    records=result_candidate_records,
                )
            except Exception as exc:
                followup_index_failure = {
                    "status": "failed",
                    "failure_reason": "followup_index_publication_failed",
                    "reason": str(exc),
                    "error_type": type(exc).__name__,
                }
                try:
                    self.store.recover_after_write_failure()
                except Exception as recovery_exc:
                    followup_index_failure.update(
                        {
                            "recovery_status": "failed",
                            "recovery_reason": str(recovery_exc),
                            "recovery_error_type": type(recovery_exc).__name__,
                        }
                    )
                else:
                    followup_index_failure["recovery_status"] = "recovered"
                try:
                    self.store.add_audit_event(
                        "followup_index_publication_failed",
                        thread_id=thread_id,
                        topic_id=turn.topic_id or "",
                        run_id=run_id,
                        ref=run_id,
                        payload=dict(followup_index_failure),
                    )
                except Exception as audit_exc:
                    followup_index_failure.update(
                        {
                            "audit_status": "failed",
                            "audit_reason": str(audit_exc),
                            "audit_error_type": type(audit_exc).__name__,
                        }
                    )
                else:
                    followup_index_failure["audit_status"] = "recorded"
        return {
            "status": "completed",
            "run_id": run_id,
            "turn_id": turn.turn_id,
            "topic_id": turn.topic_id,
            "intent": turn.turn_intent.intent,
            "topic_relation": turn.topic_relation,
            "artifact_path": result.artifact_path,
            "answer_package": package,
            "context_manifest": context_manifest,
            "accepted_graph": accepted_graph,
            "llm_calls": list(workflow_llm_calls),
            "quality_review": package.get("quality_gate") or package.get("admin_audit"),
            **(
                {"followup_index": followup_index_failure}
                if followup_index_failure is not None
                else {}
            ),
        }

    @classmethod
    def from_environment(cls) -> "ConversationAgentCore":
        store = PostgresConversationStore.from_env()
        conversation_llm_client = _conversation_llm_from_env()
        from bi_agent.runtime.analysis_runtime import AnalysisRuntime

        analysis_runtime = AnalysisRuntime.from_environment(store)
        return cls(
            store,
            conversation_llm_client=conversation_llm_client,
            release_resolver=store,
            analysis_runtime=analysis_runtime,
            evidence_resolver=analysis_runtime.evidence_resolver,
            rows_loader=analysis_runtime.rows_loader,
            evidence_writer=analysis_runtime.evidence_writer,
            runtime_registry=analysis_runtime.registry,
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


def _finalize_analysis_run_failure(
    *,
    store: Any,
    failure_reason: str,
    failure_stage: str = "",
    exc: Exception,
    run_id: str,
    thread_id: str,
    turn_id: str,
    topic_id: str,
    request: dict[str, Any],
    artifact_path: str,
    context_manifest: Mapping[str, Any],
    intent: str,
    topic_relation: str,
    llm_calls: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    failure_subreason = _safe_completed_authority_subreason(exc)
    finalized_request = {
        **_persistable_request(request),
        "failure_reason": failure_reason,
        "failure_subreason": failure_subreason,
        "failure_stage": failure_stage,
        "artifact_path": artifact_path,
    }
    primary_failure_payload = {
        "failure_subreason": failure_subreason,
        "reason": str(exc),
        "error_type": type(exc).__name__,
        "failure_stage": failure_stage,
        "artifact_path": artifact_path,
    }
    try:
        store.finalize_run_failure(
            run_id=run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            topic_id=topic_id,
            request=finalized_request,
            failure_reason=failure_reason,
            failure_stage=failure_stage,
            failure_payload=primary_failure_payload,
        )
        persisted = store.get_run_state(run_id)
        persisted_request = (
            persisted.get("request")
            if isinstance(persisted, Mapping)
            else None
        )
        if (
            not isinstance(persisted, Mapping)
            or str(persisted.get("status") or "") != "failed"
            or str(persisted.get("thread_id") or "") != thread_id
            or str(persisted.get("turn_id") or "") != turn_id
            or str(persisted.get("topic_id") or "") != topic_id
            or not isinstance(persisted_request, Mapping)
            or str(persisted_request.get("failure_reason") or "")
            != failure_reason
            or str(persisted_request.get("failure_stage") or "")
            != failure_stage
        ):
            raise EvidenceIntegrityError(
                "analysis_run_failure_terminal_state_unproven"
            )
    except Exception as persistence_exc:
        if _fresh_completed_terminal_state(
            store=store,
            run_id=run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            topic_id=topic_id,
        ):
            return _terminal_completed_conflict_result(
                store=store,
                failure_reason=failure_reason,
                failure_stage=failure_stage,
                exc=exc,
                run_id=run_id,
                thread_id=thread_id,
                turn_id=turn_id,
                topic_id=topic_id,
                artifact_path=artifact_path,
                context_manifest=context_manifest,
                intent=intent,
                topic_relation=topic_relation,
                llm_calls=llm_calls,
            )
        raise RunFailureFinalizationError(
            failure_reason=failure_reason,
            failure_stage=failure_stage,
            primary_error=exc,
            persistence_error=persistence_exc,
        ) from persistence_exc

    audit_failures = []
    for index, audit in enumerate(llm_calls, start=1):
        try:
            _record_workflow_failure_llm_audits(
                store,
                thread_id=thread_id,
                topic_id=topic_id,
                run_id=run_id,
                llm_calls=(audit,),
                start_index=index,
            )
        except Exception as audit_exc:
            audit_failures.append(
                {
                    "index": index,
                    "error_type": type(audit_exc).__name__,
                    "reason": str(audit_exc),
                }
            )
    return {
        "status": "failed",
        "run_id": run_id,
        "turn_id": turn_id,
        "topic_id": topic_id,
        "intent": intent,
        "topic_relation": topic_relation,
        "artifact_path": artifact_path,
        "context_manifest": dict(context_manifest),
        "failure_reason": failure_reason,
        "failure_subreason": failure_subreason,
        "failure_stage": failure_stage,
        "llm_calls": list(llm_calls),
        **(
            {
                "audit_persistence": {
                    "status": "partial",
                    "failures": audit_failures,
                }
            }
            if audit_failures
            else {"audit_persistence": {"status": "recorded"}}
        ),
    }


def _terminal_completed_conflict_result(
    *,
    store: Any,
    failure_reason: str,
    exc: Exception,
    run_id: str,
    thread_id: str,
    turn_id: str,
    topic_id: str,
    artifact_path: str,
    context_manifest: Mapping[str, Any],
    intent: str,
    topic_relation: str,
    llm_calls: tuple[dict[str, Any], ...],
    failure_stage: str = "",
) -> dict[str, Any]:
    failure_subreason = _safe_completed_authority_subreason(exc)
    primary_payload = {
        "failure_subreason": failure_subreason,
        "reason": str(exc),
        "error_type": type(exc).__name__,
        "failure_stage": failure_stage,
        "artifact_path": artifact_path,
        "durable_run_status": "completed",
    }
    try:
        store.record_terminal_completion_conflict(
            run_id=run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            topic_id=topic_id,
            failure_reason=failure_reason,
            payload=primary_payload,
        )
    except Exception as persistence_exc:
        raise RunFailureFinalizationError(
            failure_reason=failure_reason,
            failure_stage=failure_stage,
            primary_error=exc,
            persistence_error=persistence_exc,
        ) from persistence_exc

    audit_failures = []
    for index, audit in enumerate(llm_calls, start=1):
        try:
            _record_workflow_failure_llm_audits(
                store,
                thread_id=thread_id,
                topic_id=topic_id,
                run_id=run_id,
                llm_calls=(audit,),
                start_index=index,
            )
        except Exception as audit_exc:
            audit_failures.append(
                {
                    "index": index,
                    "error_type": type(audit_exc).__name__,
                    "reason": str(audit_exc),
                }
            )
    return {
        "status": "failed",
        "run_id": run_id,
        "turn_id": turn_id,
        "topic_id": topic_id,
        "intent": intent,
        "topic_relation": topic_relation,
        "artifact_path": artifact_path,
        "context_manifest": dict(context_manifest),
        "failure_reason": failure_reason,
        "failure_subreason": failure_subreason,
        "failure_stage": failure_stage,
        "durable_run_status": "completed",
        "llm_calls": list(llm_calls),
        **(
            {
                "audit_persistence": {
                    "status": "partial",
                    "failures": audit_failures,
                }
            }
            if audit_failures
            else {"audit_persistence": {"status": "recorded"}}
        ),
    }


def _recover_completed_material_authority(
    *,
    store: Any,
    run_id: str,
    thread_id: str,
    turn_id: str,
    topic_id: str,
    expected_material_authority: Mapping[str, Any],
) -> str:
    try:
        store.recover_after_write_failure()
        state = store.get_run_state(run_id)
    except Exception:
        return "unproven"
    ownership = _completion_state_ownership(
        state=state,
        thread_id=thread_id,
        turn_id=turn_id,
        topic_id=topic_id,
    )
    if ownership == "nonterminal":
        return "nonterminal"
    if ownership != "completed":
        return "unproven"
    try:
        authority = store.resolve_completed_material_authority(
            source_run_id=run_id,
            thread_id=thread_id,
            topic_id=topic_id,
        )
    except Exception:
        return "terminal_completed_conflict"
    if canonical_value(authority.get("material_authority") or {}) == (
        canonical_value(expected_material_authority)
    ):
        return "completed_exact"
    return "terminal_completed_conflict"


def _recover_generic_completion(
    *,
    store: Any,
    run_id: str,
    thread_id: str,
    turn_id: str,
    topic_id: str,
    expected_request: Mapping[str, Any],
) -> str:
    try:
        store.recover_after_write_failure()
        state = store.get_run_state(run_id)
    except Exception:
        return "unproven"
    if not isinstance(state, Mapping):
        return "unproven"
    ownership = _completion_state_ownership(
        state=state,
        thread_id=thread_id,
        turn_id=turn_id,
        topic_id=topic_id,
    )
    if ownership == "nonterminal":
        return "nonterminal"
    if ownership != "completed":
        return "unproven"
    if canonical_value(state.get("request") or {}) == canonical_value(
        _persistable_request(dict(expected_request))
    ):
        return "completed_exact"
    return "terminal_completed_conflict"


def _completion_state_ownership(
    *,
    state: Any,
    thread_id: str,
    turn_id: str,
    topic_id: str,
) -> str:
    if not isinstance(state, Mapping):
        return "unproven"
    status = str(state.get("status") or "")
    if str(state.get("thread_id") or "") != thread_id:
        return "unproven"
    current_turn_id = str(state.get("turn_id") or "")
    current_topic_id = str(state.get("topic_id") or "")
    if status == "completed":
        return (
            "completed"
            if current_turn_id == turn_id and current_topic_id == topic_id
            else "unproven"
        )
    if status in {"queued", "running", "running_workflow"}:
        return (
            "nonterminal"
            if current_turn_id in {"", turn_id}
            and current_topic_id in {"", topic_id}
            else "unproven"
        )
    return "unproven"


def _fresh_completed_terminal_state(
    *,
    store: Any,
    run_id: str,
    thread_id: str,
    turn_id: str,
    topic_id: str,
) -> bool:
    try:
        state = store.get_run_state(run_id)
    except Exception:
        return False
    return _completion_state_ownership(
        state=state,
        thread_id=thread_id,
        turn_id=turn_id,
        topic_id=topic_id,
    ) == "completed"


def _safe_completed_authority_subreason(exc: Exception) -> str:
    if isinstance(exc, EvidenceIntegrityError):
        reason = str(exc).strip()
        if re.fullmatch(r"[a-z][a-z0-9_.:-]*", reason):
            return reason
    return type(exc).__name__


def _analysis_runtime_failure_reason(stage: str) -> str:
    return {
        "material_authority_projection": "material_authority_projection_failed",
        "runtime_bundle_validation": "analysis_runtime_bundle_validation_failed",
        "artifact_synchronization": "analysis_runtime_artifact_sync_failed",
        "store_commit": "analysis_runtime_store_commit_failed",
    }.get(stage, "analysis_runtime_publication_failed")


def _preflight_analysis_runtime_bundle(
    *,
    store: Any,
    run_id: str,
    records: Mapping[str, Any],
) -> None:
    from bi_agent.runtime.runtime_persistence import (
        validate_analysis_runtime_records,
    )

    validate_analysis_runtime_records(
        run_id=run_id,
        result_candidate_resolver=getattr(
            store,
            "resolve_result_candidate_authority",
            None,
        ),
        **records,
    )


def _conversation_llm_from_env() -> Any:
    from bi_agent.runtime.llm_client import (
        LLMConfigurationError,
        OpenAICompatibleLLMClient,
    )

    try:
        return OpenAICompatibleLLMClient.from_env()
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


def _publish_result_reuse_candidates(
    store: Any,
    *,
    topic_id: str,
    run_id: str,
    request: Mapping[str, Any],
    records: Mapping[str, Any],
) -> tuple[str, ...]:
    if not topic_id:
        return ()
    analysis = _authority_mapping(records.get("analysis_contract"))
    analysis_ref = str(analysis.get("analysis_contract_id") or "")
    analysis_signature = analysis_contract_signature(analysis) if analysis else ""
    if (
        not analysis_ref
        or not analysis_signature
        or str(analysis.get("contract_signature") or "") != analysis_signature
    ):
        return ()
    context_manifest = _authority_mapping(request.get("context_manifest"))
    runtime_snapshot_id = str(context_manifest.get("snapshot_version") or "")
    runtime_contract_version = str(
        _authority_mapping(context_manifest.get("contract_versions")).get("runtime")
        or ""
    )
    permission_scope = str(analysis.get("permission_scope") or "")
    if not runtime_snapshot_id or not runtime_contract_version or not permission_scope:
        return ()

    query_records = {
        str(_authority_value(record, "result_ref") or ""): record
        for record in records.get("query_execution_records") or ()
    }
    rows_records = {
        str(_authority_value(record, "rows_ref") or ""): record
        for record in records.get("rows_records") or ()
    }
    snapshot_records = {
        str(_authority_value(record, "snapshot_ref") or ""): record
        for record in records.get("snapshot_records") or ()
    }
    completeness_by_result: dict[str, list[Any]] = {}
    for record in records.get("completeness_records") or ():
        completeness_by_result.setdefault(
            str(_authority_value(record, "result_ref") or ""), []
        ).append(record)
    bindings = tuple(records.get("capability_binding_records") or ())
    verified_claims = tuple(
        _authority_mapping(claim) for claim in records.get("verified_claims") or ()
    )
    claim_result_refs = tuple(
        dict.fromkeys(
            str(result_ref)
            for claim in verified_claims
            for result_ref in claim.get("result_refs") or ()
            if result_ref
        )
    )
    published: list[str] = []
    semantic_scope_signature = f"analysis-contract:sha256:{analysis_signature}"
    for result_ref in claim_result_refs:
        query = query_records.get(result_ref)
        if query is None or str(_authority_value(query, "execution_status") or "") != "succeeded":
            continue
        rows_ref = str(_authority_value(query, "rows_ref") or "")
        rows = rows_records.get(rows_ref)
        if (
            rows is None
            or str(_authority_value(rows, "rows_content_hash") or "")
            != str(_authority_value(query, "rows_content_hash") or "")
        ):
            continue
        claim_completeness_refs = {
            str(ref)
            for claim in verified_claims
            if result_ref in tuple(str(item) for item in claim.get("result_refs") or ())
            for ref in claim.get("completeness_record_refs") or ()
        }
        completeness = tuple(
            sorted(
                (
                    record
                    for record in completeness_by_result.get(result_ref, ())
                    if str(_authority_value(record, "record_ref") or "")
                    in claim_completeness_refs
                    and str(
                        _authority_mapping(
                            _authority_value(record, "report_payload")
                        ).get("completeness_status")
                        or ""
                    )
                    == "complete"
                    and str(
                        _authority_mapping(
                            _authority_value(record, "report_payload")
                        ).get("analysis_readiness")
                        or ""
                    )
                    == "ready"
                ),
                key=lambda record: str(
                    _authority_value(record, "record_ref") or ""
                ),
            )
        )
        if not completeness:
            continue
        ready_bindings = tuple(
            sorted(
                (
                    binding
                    for binding in bindings
                    if str(_authority_value(binding, "status") or "") == "ready"
                    and str(
                        _authority_value(binding, "analysis_contract_ref") or ""
                    )
                    == analysis_ref
                    and _binding_supports_candidate(
                        binding, query, rows, completeness
                    )
                ),
                key=lambda binding: str(
                    _authority_value(binding, "record_ref") or ""
                ),
            )
        )
        if not ready_bindings:
            continue
        source_snapshot_refs = tuple(
            str(ref)
            for ref in _authority_value(query, "source_snapshot_refs") or ()
        )
        source_snapshot_record_refs = tuple(
            str(ref)
            for ref in _authority_value(query, "source_snapshot_record_refs") or ()
        )
        source_snapshot_record_digests = tuple(
            str(ref)
            for ref in _authority_value(query, "source_snapshot_record_digests") or ()
        )
        if not source_snapshot_refs or not (
            len(source_snapshot_refs)
            == len(source_snapshot_record_refs)
            == len(source_snapshot_record_digests)
        ):
            continue
        snapshots: list[Mapping[str, Any]] = []
        valid_snapshots = True
        for index, snapshot_ref in enumerate(source_snapshot_refs):
            snapshot_record = snapshot_records.get(snapshot_ref)
            if (
                snapshot_record is None
                or str(_authority_value(snapshot_record, "record_ref") or "")
                != source_snapshot_record_refs[index]
                or str(_authority_value(snapshot_record, "record_digest") or "")
                != source_snapshot_record_digests[index]
            ):
                valid_snapshots = False
                break
            snapshot_payload = _authority_mapping(
                _authority_value(snapshot_record, "payload")
            )
            if not all(
                str(snapshot_payload.get(field_name) or "")
                for field_name in (
                    "release_ref",
                    "authority_record_ref",
                    "schema_fingerprint",
                )
            ):
                valid_snapshots = False
                break
            if permission_scope not in tuple(
                str(scope)
                for scope in snapshot_payload.get("permission_scopes") or ()
            ):
                valid_snapshots = False
                break
            snapshots.append(snapshot_payload)
        if not valid_snapshots:
            continue
        candidate = sign_result_reuse_candidate(
            {
                "schema_version": "result-reuse-candidate.v1",
                "source_run_id": run_id,
                "result_ref": result_ref,
                "query_contract_ref": str(
                    _authority_value(query, "query_contract_ref") or ""
                ),
                "query_contract_signature": str(
                    _authority_value(query, "contract_signature") or ""
                ),
                "query_execution_record_ref": str(
                    _authority_value(query, "record_ref") or ""
                ),
                "query_execution_record_digest": str(
                    _authority_value(query, "record_digest") or ""
                ),
                "analysis_contract_ref": analysis_ref,
                "analysis_contract_signature": analysis_signature,
                "runtime_snapshot_id": runtime_snapshot_id,
                "runtime_contract_version": runtime_contract_version,
                "source_snapshot_refs": list(source_snapshot_refs),
                "source_snapshot_record_refs": list(source_snapshot_record_refs),
                "source_snapshot_record_digests": list(
                    source_snapshot_record_digests
                ),
                "source_release_refs": [
                    str(snapshot["release_ref"]) for snapshot in snapshots
                ],
                "source_release_authority_refs": [
                    str(snapshot["authority_record_ref"]) for snapshot in snapshots
                ],
                "source_schema_fingerprints": [
                    str(snapshot["schema_fingerprint"]) for snapshot in snapshots
                ],
                "permission_scope": permission_scope,
                "semantic_scope_signature": semantic_scope_signature,
                "rows_ref": rows_ref,
                "rows_record_ref": str(
                    _authority_value(rows, "record_ref") or ""
                ),
                "rows_record_digest": str(
                    _authority_value(rows, "record_digest") or ""
                ),
                "rows_content_hash": str(
                    _authority_value(rows, "rows_content_hash") or ""
                ),
                "completeness_report_ref": str(
                    _authority_value(query, "completeness_report_ref") or ""
                ),
                "completeness_record_refs": [
                    str(_authority_value(record, "record_ref") or "")
                    for record in completeness
                ],
                "completeness_record_digests": [
                    str(_authority_value(record, "report_digest") or "")
                    for record in completeness
                ],
                "binding_record_refs": [
                    str(_authority_value(binding, "record_ref") or "")
                    for binding in ready_bindings
                ],
                "binding_record_digests": [
                    str(_authority_value(binding, "binding_digest") or "")
                    for binding in ready_bindings
                ],
            }
        )
        store.add_result_ref(
            topic_id,
            result_ref=result_ref,
            snapshot_id=runtime_snapshot_id,
            contract_version=runtime_contract_version,
            permission_scope=permission_scope,
            semantic_scope=semantic_scope_signature,
            payload=candidate,
        )
        published.append(result_ref)
    return tuple(published)


def _binding_supports_candidate(
    binding: Any,
    query: Any,
    rows: Any,
    completeness: tuple[Any, ...],
) -> bool:
    result_ref = str(_authority_value(query, "result_ref") or "")
    groups = (
        (
            "result_refs",
            "query_execution_record_refs",
            "query_execution_record_digests",
            "rows_refs",
            "rows_metadata_record_refs",
            "rows_metadata_record_digests",
            "rows_content_hashes",
            "completeness_report_refs",
            "completeness_record_refs",
            "completeness_record_digests",
        ),
        (
            "validation_result_refs",
            "validation_query_execution_record_refs",
            "validation_query_execution_record_digests",
            "validation_rows_refs",
            "validation_rows_metadata_record_refs",
            "validation_rows_metadata_record_digests",
            "validation_rows_content_hashes",
            "validation_completeness_report_refs",
            "validation_completeness_record_refs",
            "validation_completeness_record_digests",
        ),
    )
    expected_completeness = {
        (
            str(_authority_value(record, "record_ref") or ""),
            str(_authority_value(record, "report_digest") or ""),
        )
        for record in completeness
    }
    for fields in groups:
        result_refs = tuple(
            str(ref) for ref in _authority_value(binding, fields[0]) or ()
        )
        if result_ref not in result_refs:
            continue
        index = result_refs.index(result_ref)
        aligned = [tuple(_authority_value(binding, field_name) or ()) for field_name in fields[1:]]
        if any(index >= len(values) for values in aligned):
            continue
        actual = tuple(str(values[index]) for values in aligned)
        if actual[:7] != (
            str(_authority_value(query, "record_ref") or ""),
            str(_authority_value(query, "record_digest") or ""),
            str(_authority_value(rows, "rows_ref") or ""),
            str(_authority_value(rows, "record_ref") or ""),
            str(_authority_value(rows, "record_digest") or ""),
            str(_authority_value(rows, "rows_content_hash") or ""),
            str(_authority_value(query, "completeness_report_ref") or ""),
        ):
            continue
        if (actual[7], actual[8]) in expected_completeness:
            return True
    return False


def _authority_value(record: Any, field_name: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(field_name)
    return getattr(record, field_name, None)


def _conversation_entry_failure_reason(exc: Exception) -> str:
    if isinstance(
        exc,
        (ConversationOrchestrationError, EvidenceIntegrityError),
    ):
        reason = str(exc).strip()
        if re.fullmatch(r"[a-z][a-z0-9_]*(?::[a-z0-9_,.=-]+)*", reason):
            return reason
    return "conversation_orchestration_failed"


def _authority_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _build_clarification_source_envelope(
    *,
    source_run_id: str,
    source_thread_id: str,
    source_topic_id: str,
    source_owner_id: str,
    question: str,
    analysis_context: Mapping[str, Any],
    accepted_graph: Any = (),
    analysis_contract: Mapping[str, Any] | None = None,
    analysis_route: Mapping[str, Any] | None = None,
    original_intent: Mapping[str, Any] | None = None,
    material_slots: Mapping[str, Any] | None = None,
    clarification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(question, str) or not question.strip():
        raise ConversationOrchestrationError(
            "clarification_source_envelope_invalid"
        )
    normalized_question = question.strip()
    envelope = {
        "schema_version": "clarification-source-envelope.v1",
        "source_run_id": source_run_id,
        "source_thread_id": source_thread_id,
        "source_topic_id": source_topic_id,
        "source_owner_id": source_owner_id,
        "question": normalized_question,
        "analysis_context": dict(analysis_context),
        "source_material": {
            "accepted_graph": list(accepted_graph or ()),
            "analysis_contract": dict(analysis_contract or {}),
            "analysis_route": dict(analysis_route or {}),
            "original_intent": dict(original_intent or {}),
            "material_slots": dict(material_slots or {}),
        },
        "clarification": dict(clarification or {}),
    }
    return {**envelope, "source_digest": canonical_digest(envelope)}


def _persistable_request(request: dict[str, Any]) -> dict[str, Any]:
    safe = dict(request or {})
    for key in (
        "row_provider",
        "llm_client",
        "evidence_resolver",
        "rows_loader",
        "evidence_writer",
        "runtime_registry",
        "release_resolver",
        "analysis_runtime",
    ):
        if key in safe:
            safe[key] = _runtime_object_descriptor(safe[key])
    runtime = safe.get("runtime")
    if isinstance(runtime, dict):
        safe_runtime = dict(runtime)
        for key in (
            "row_provider",
            "llm_client",
            "evidence_resolver",
            "rows_loader",
            "evidence_writer",
            "runtime_registry",
            "release_resolver",
            "analysis_runtime",
        ):
            if key in safe_runtime:
                safe_runtime[key] = _runtime_object_descriptor(safe_runtime[key])
        safe["runtime"] = safe_runtime
    return safe


def _runtime_object_descriptor(value: Any) -> dict[str, str]:
    return {
        "type": value.__class__.__name__,
        "module": value.__class__.__module__,
    }


def _clarification_choice_from_answer(
    user_message: str,
    intent: str,
    *,
    explicit_choice: dict[str, Any] | None = None,
    selected_material_action: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    material_action = dict(selected_material_action or {})
    if material_action:
        if str(material_action.get("action_kind") or "") != (
            "bind_material_choice"
        ):
            raise EvidenceIntegrityError(
                "clarification_material_action_invalid"
            )
        material_patch = material_action.get("material_patch")
        if not isinstance(material_patch, Mapping) or not material_patch:
            raise EvidenceIntegrityError(
                "clarification_material_action_invalid"
            )
        return {
            "answer_text": user_message,
            **dict(material_patch),
        }
    if explicit_choice and not any(
        key in explicit_choice
        for key in ("runId", "answer", "selectedOptionId", "source")
    ):
        return dict(explicit_choice)
    if intent != "clarification_answer":
        return {}
    answer_text = str(
        (explicit_choice or {}).get("answer") or user_message
    ).strip()
    choice: dict[str, Any] = {"answer_text": answer_text}
    if _looks_like_daily_outlier_removal_choice(user_message):
        choice.update(
            {
                "outlier_removal_strategy": "daily_remove_top_positive_day",
                "period_grain": "day",
                "removal_policy": "top_positive_contribution_periods",
                "max_removed_periods": 1,
            }
        )
    return choice


def _clarification_option_id(
    clarification: Mapping[str, Any],
    label: str,
    index: int,
) -> str:
    matching_ids = tuple(
        str(action.get("choice_id") or "")
        for action in clarification.get("choice_actions") or ()
        if isinstance(action, Mapping)
        and clarification_labels_match(
            action.get("business_label") or action.get("business_semantics"),
            label,
        )
        and str(action.get("choice_id") or "")
    )
    if len(matching_ids) > 1:
        raise EvidenceIntegrityError("clarification_choice_action_conflict")
    return matching_ids[0] if matching_ids else f"query-gap-{index + 1}"


def _looks_like_daily_outlier_removal_choice(text: str) -> bool:
    return (
        any(token in text for token in ("移除", "剔除", "排除", "去掉", "排掉"))
        and any(token in text for token in ("按日", "按天", "日期", "天", "日"))
        and any(token in text for token in ("复算", "贡献最大", "最大正向"))
    )


def _authority_closed_degradation_choice(
    choice: Mapping[str, Any],
    authority: Mapping[str, Any],
    registry: RuntimeContractRegistry | None,
) -> dict[str, Any]:
    action_kind = str(choice.get("action_kind") or "")
    if action_kind not in {
        "omit_unavailable_context",
        "continue_with_boundary_only",
    }:
        return dict(choice)
    source = authority.get("analysis_contract")
    if not isinstance(source, Mapping):
        return dict(choice)
    try:
        contract_payload = dict(source)
        stored_signature = str(
            contract_payload.pop("contract_signature", "") or ""
        )
        contract = analysis_contract_from_dict(contract_payload)
        if stored_signature and stored_signature != analysis_contract_signature(
            contract
        ):
            return dict(choice)
    except (KeyError, TypeError, ValueError):
        return dict(choice)
    registry = registry or RuntimeContractRegistry.from_path(
        CANONICAL_RUNTIME_BINDINGS_PATH
    )
    target_metrics = tuple(
        dict.fromkeys(binding.metric_id for binding in contract.metric_bindings)
    ) or tuple(
        metric
        for metric in contract.target_metric_refs
        if metric in registry.metric_ids
    )
    if not target_metrics:
        return dict(choice)
    resolution = resolve_analysis_obligations(
        ObligationRequest(
            question_families=contract.question_families,
            diagnostic_tags=(),
            target_metrics=target_metrics,
            requested_dimensions=tuple(
                binding.dimension_id for binding in contract.dimension_bindings
            ),
            baselines=tuple(
                window.window_id
                for window in contract.resolved_windows
                if window.role != "target"
            ),
            context_sources=(),
            claim_intents=contract.claim_intents,
        ),
        registry,
    )
    obligation_capabilities = set(
        (
            *resolution.required_capabilities,
            *resolution.conditional_capabilities,
            *resolution.independent_capabilities,
        )
    )
    obligation_capabilities.update(contract.capability_requirements)
    nonready = {
        capability
        for gap in contract.contract_gaps
        for capability in gap.affected_capabilities
        if capability in obligation_capabilities
    }
    affected = [
        capability
        for capability in contract.capability_requirements
        if capability in nonready
    ]
    if not affected:
        return dict(choice)
    return {**dict(choice), "affected_capabilities": affected}


def _manifest_with_accepted_choice(
    manifest: Mapping[str, Any],
    choice: Mapping[str, Any],
) -> dict[str, Any]:
    updated = dict(manifest)
    updated["accepted_assumptions"] = [dict(choice)]
    return _derived_context_manifest(manifest, updated)


def _derived_context_manifest(
    parent: Mapping[str, Any],
    updated: Mapping[str, Any],
) -> dict[str, Any]:
    canonical_parent = canonical_value(parent)
    child = canonical_value(updated)
    child_payload = {
        key: value for key, value in child.items() if key != "manifest_id"
    }
    parent_payload = {
        key: value
        for key, value in canonical_parent.items()
        if key != "manifest_id"
    }
    if child_payload == parent_payload:
        return canonical_parent
    child["manifest_id"] = (
        "context-"
        + canonical_digest(
            {
                "parent_manifest_id": str(parent.get("manifest_id") or ""),
                "manifest": child_payload,
            }
        )[:12]
    )
    return child


def _manifest_with_current_run_evidence(
    manifest: dict[str, Any],
    package: dict[str, Any],
    role: str,
) -> dict[str, Any]:
    refs = _claim_evidence_refs(package)
    if not refs:
        return manifest
    updated = dict(manifest)
    items = list(updated.get("items") or [])
    existing = {str(item.get("source_ref")) for item in items if isinstance(item, dict)}
    snapshot = str(package.get("snapshot_id") or package.get("snapshot") or "current-run")
    for ref in refs:
        if ref in existing:
            continue
        items.append(
            {
                "source_type": "evidence",
                "source_ref": ref,
                "summary": "本轮 workflow 产出的可审计证据引用。",
                "can_support_claims": True,
                "visibility": role,
                "reason": "current_run_evidence",
                "permission_scope": role,
                "source_version": snapshot,
                "expired": False,
                "claim_use": "evidence",
            }
        )
    updated["items"] = items
    updated["can_support_claims"] = True
    claim_use_policy = dict(updated.get("claim_use_policy") or {})
    claim_use_policy["can_support_bi_claim"] = True
    updated["claim_use_policy"] = claim_use_policy
    return _derived_context_manifest(manifest, updated)


def _claim_evidence_refs(package: dict[str, Any]) -> list[str]:
    evidence_refs = _package_evidence_refs(package)
    refs: list[str] = []
    for section in package.get("sections", []):
        payload = section.get("payload", {}) if isinstance(section, dict) else {}
        claims = payload.get("claims", [])
        if not isinstance(claims, list):
            continue
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            for ref in claim.get("evidence_refs", []):
                ref = str(ref)
                if ref and ref in evidence_refs and ref not in refs:
                    refs.append(ref)
    return refs


def _package_evidence_refs(package: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for section in package.get("sections", []):
        payload = section.get("payload", {}) if isinstance(section, dict) else {}
        evidence_items = payload.get("evidence", [])
        if not isinstance(evidence_items, list):
            continue
        for item in evidence_items:
            if isinstance(item, dict) and item.get("evidence_ref"):
                refs.add(str(item["evidence_ref"]))
    return refs


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--role", default="analyst")
    parser.add_argument("--runtime-permission-scope")
    parser.add_argument("--artifact-root", default="artifacts/phase-7")
    parser.add_argument("--clarification")
    parser.add_argument("--clarification-dispatch-source-run-id")
    parser.add_argument("--clarification-dispatch-owner-id")
    parser.add_argument("--dispatch-owner-id")
    parser.add_argument("--dispatch-lease-epoch", type=int)
    parser.add_argument("--prior-analysis-assets")
    parser.add_argument("--as-of")
    args = parser.parse_args(argv)
    clarification = json.loads(args.clarification) if args.clarification else None
    prior_analysis_assets = _parse_prior_analysis_assets(args.prior_analysis_assets)
    if bool(args.clarification_dispatch_source_run_id) != bool(
        args.clarification_dispatch_owner_id
    ):
        parser.error(
            "clarification dispatch source run id and owner id must be provided together"
        )
    clarification_dispatch = (
        {
            "source_run_id": args.clarification_dispatch_source_run_id,
            "dispatch_owner_id": args.clarification_dispatch_owner_id,
        }
        if args.clarification_dispatch_source_run_id
        else None
    )
    if bool(args.dispatch_owner_id) != bool(args.dispatch_lease_epoch):
        parser.error(
            "dispatch owner id and positive lease epoch must be provided together"
        )
    run_dispatch = (
        {
            "dispatch_owner_id": args.dispatch_owner_id,
            "lease_epoch": args.dispatch_lease_epoch,
        }
        if args.dispatch_owner_id
        else None
    )

    core = ConversationAgentCore.from_environment()
    result = core.run_message(
        thread_id=args.thread_id,
        run_id=args.run_id,
        user_message=args.message,
        role=args.role,
        runtime_permission_scope=args.runtime_permission_scope,
        artifact_root=args.artifact_root,
        clarification=clarification,
        clarification_dispatch=clarification_dispatch,
        run_dispatch=run_dispatch,
        prior_analysis_assets=prior_analysis_assets,
        analysis_context={"as_of": args.as_of} if args.as_of else None,
    )
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result["status"] in {
        "completed",
        "completed_without_workflow",
        "waiting_for_clarification",
    } else 1


def _parse_prior_analysis_assets(raw: str | None) -> tuple[Mapping[str, Any], ...]:
    if not raw:
        return ()
    data = json.loads(raw)
    if isinstance(data, Mapping):
        return (dict(data),)
    if isinstance(data, list):
        return tuple(dict(item) for item in data if isinstance(item, Mapping))
    return ()


if __name__ == "__main__":
    raise SystemExit(main())
