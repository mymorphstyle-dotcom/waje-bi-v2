from __future__ import annotations

from dataclasses import dataclass

from datetime import datetime, timezone

import re

from time import perf_counter

from typing import Any, Callable, Mapping, Optional, Sequence, TypedDict

from langgraph.graph import END, StateGraph

from bi_agent.conversation.models import CLARIFICATION_ESCAPE_OPTION

from bi_agent.runtime.canonical_values import to_jsonable

from bi_agent.runtime.baseline_semantics import (
    CANONICAL_BASELINE_IDS,
    baseline_llm_semantics,
)

from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)

from bi_agent.runtime.factor_coverage import (
    FactorCoveragePlan,
    FactorCoverageResult,
    build_investigation_branches,
    compile_factor_coverage_plan,
    settle_factor_coverage,
    synthesize_factor_coverage,
)

from bi_agent.runtime.durable_call_journal import (
    DurableCallJournal,
    DurableCallJournalError,
    DurableProviderClient,
)

from bi_agent.runtime.authority_context_resolver import resolve_latest_authority_context

from bi_agent.runtime.authoritative_plan_result import (
    AUTHORITATIVE_PLAN_RESULT_SCHEMA_VERSION,
    planner_raw_response_ref,
    validate_planner_provider_audit_closure,
)

from bi_agent.runtime.authoritative_execution_result import (
    AuthoritativeExecutionResult,
    validate_typed_authoritative_execution_result,
)

from bi_agent.runtime.capability_authority import (
    ExecutionSnapshot,
    ExplorationStopRecord,
)

from bi_agent.runtime.capability_scheduler import (
    capability_execution_transition_input,
    execute_capability_plan,
)

from bi_agent.runtime.capability_task_adapter import builtin_capability_adapter_registry

from bi_agent.runtime.claim_coverage import (
    PLAN_EXPANSION_PROVIDER_TASK,
    ClaimCoverageCheckpoint,
    ClaimCoverageContractError,
    PlanExpansionDecision,
    PlanPatch,
    claim_coverage_transition_payloads,
    evaluate_claim_coverage,
)

from bi_agent.runtime.llm_client import LLMOutputError
from bi_agent.runtime.llm_contract_projection import (
    ContractProjection,
    project_mapping_fields,
)

from bi_agent.runtime.llm_prompts import build_prompt

from bi_agent.runtime.plan_authority import (
    AuthorityContext,
    PlanAuthorityContractError,
    PlannerProposal,
    PlanRevision,
    ProposalAdmissionRecord,
)

from bi_agent.runtime.plan_compiler import AuthoritativePlanCompiler

from bi_agent.runtime.narrative_authority import PublicationFieldVisibilityPolicy

from bi_agent.runtime.post_execution_workflow import (
    PostExecutionWorkflowResult,
    run_post_execution_workflow,
)

from bi_agent.runtime.publication_safety import FixedSensitiveOutputInspector

from bi_agent.runtime.query_ir import (
    QueryIRContractError,
    compile_query_bundle,
    settle_query_bundle,
)

from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)

from bi_agent.runtime.single_authority import (
    DecisionLedger,
    DecisionRecord,
    DurableTransition,
    IntentRevision,
    LifecycleState,
)

from bi_agent.runtime.temporal_comparison import (
    COMPARISON_WINDOW_VALUE_REFS,
    MONTH_PHASE_DEFINITION_VALUE_REFS,
    PHASE_AGGREGATION_VALUE_REFS,
    TemporalComparisonContractError,
    calendar_partition_llm_contracts,
    normalize_temporal_decision_value,
    resolve_effective_comparison,
    target_bounds,
    temporal_decision_option_id,
    validate_comparison_spec,
)

DEFAULT_LLM_TASK_PROFILE = ("default", "disabled")
_COMPARISON_INTERPRETATION_VALUE_REFS = (
    "interpretation_1",
    "interpretation_2",
    "interpretation_3",
)

LLM_TASK_PROFILES: dict[str, tuple[str, str]] = {
    "single_authority_intent": ("default", "enabled"),
    "single_authority_clarification": ("default", "enabled"),
    "single_authority_decision_binding": ("critical", "enabled"),
    "single_authority_plan_proposal": ("critical", "disabled"),
    PLAN_EXPANSION_PROVIDER_TASK: ("critical", "enabled"),
    "single_authority_plan_patch_proposal": ("critical", "enabled"),
}

_DURABLE_PROVIDER_STAGE_BY_TASK: dict[str, tuple[str, str]] = {
    "single_authority_intent": ("intent_provider", "bind_intent"),
    "single_authority_clarification": (
        "clarification_provider",
        "generate_clarification",
    ),
    "single_authority_plan_proposal": (
        "planner_provider",
        "compile_authoritative_plan",
    ),
    PLAN_EXPANSION_PROVIDER_TASK: (
        "semantic_provider",
        "evaluate_claim_coverage",
    ),
    "single_authority_plan_patch_proposal": (
        "plan_patch_provider",
        "compile_plan_patch",
    ),
}

WORKFLOW_REQUEST_FIELDS = frozenset(
    {
        "run_id",
        "run_attempt_id",
        "question",
        "thread_id",
        "authority_store",
        "llm_client",
        "stop_after_phase",
        "recursion_limit",
        "supersedes_intent_revision_id",
        "superseded_plan_fields",
        "intent_revision_reason_ref",
        "parent_transition_id",
        "runtime_registry",
        "release_resolver",
        "analysis_runtime",
        "owner_ref",
        "authority_connection",
        "locale",
        "destination_ref",
        "publication_channel",
        "delivery_transport",
        "controlled_investigation_enabled",
    }
)
_WORKFLOW_REQUIRED_REQUEST_FIELDS = frozenset({"question", "authority_store"})
_MATERIAL_REVISION_REQUEST_FIELDS = frozenset(
    {
        "supersedes_intent_revision_id",
        "superseded_plan_fields",
        "intent_revision_reason_ref",
        "parent_transition_id",
    }
)
_POST_EXECUTION_REQUEST_FIELDS = frozenset(
    {
        "thread_id",
        "owner_ref",
        "authority_connection",
        "locale",
        "destination_ref",
        "publication_channel",
        "delivery_transport",
    }
)


class WorkflowState(TypedDict, total=False):
    request: dict[str, Any]
    run_id: str
    checkpoint_events: list[dict[str, Any]]
    llm_calls: list[dict[str, Any]]
    llm_client: Any
    provider_attempt_refs: dict[str, tuple[str, ...]]
    raw_provider_outputs: dict[str, dict[str, Any]]
    intent: dict[str, Any]
    intent_revision: dict[str, Any]
    raw_intent_output: dict[str, Any]
    raw_clarification_output: dict[str, Any]
    decision_ledger: list[dict[str, Any]]
    decision_ledger_position: int
    durable_transition_id: str
    durable_checkpoint: dict[str, Any]
    decision_options: list[dict[str, Any]]
    boundary_decision: dict[str, Any]
    clarification_outcome: dict[str, Any]
    authority_context: dict[str, Any]
    planner_proposal: dict[str, Any]
    proposal_admission_record: dict[str, Any]
    plan_revision: dict[str, Any]
    plan_result: dict[str, Any]
    query_bundle: dict[str, Any]
    execution_snapshot: dict[str, Any]
    exploration_stop_record: dict[str, Any]
    execution_result: dict[str, Any]
    authoritative_execution_result: AuthoritativeExecutionResult
    factor_coverage_plan: dict[str, Any]
    factor_coverage_result: dict[str, Any]
    investigation_branches: list[dict[str, Any]]
    investigation_synthesis: dict[str, Any]
    claim_coverage_evaluation: dict[str, Any]
    plan_expansion_decision: dict[str, Any]
    plan_patch: dict[str, Any] | None
    claim_coverage_checkpoint: ClaimCoverageCheckpoint
    interaction_result: dict[str, Any]
    post_execution_result: PostExecutionWorkflowResult
    workflow_status: str


@dataclass(frozen=True)
class WorkflowRunResult:
    status: str
    run_id: str
    interaction_result: Optional[dict[str, Any]] = None
    plan_result: Optional[dict[str, Any]] = None
    execution_result: Optional[dict[str, Any]] = None
    factor_coverage_plan: Optional[dict[str, Any]] = None
    factor_coverage_result: Optional[dict[str, Any]] = None
    investigation_branches: tuple[dict[str, Any], ...] = ()
    investigation_synthesis: Optional[dict[str, Any]] = None
    post_execution_result: Optional[PostExecutionWorkflowResult] = None
    failure_reason: str = ""
    checkpoint_events: tuple[dict[str, Any], ...] = ()
    llm_calls: tuple[dict[str, Any], ...] = ()


class WorkflowFailure(Exception):
    def __init__(self, message: str, *, failure_type: str = "technical"):
        super().__init__(message)
        self.failure_type = failure_type


def _exception_reason(exc: BaseException) -> str:
    return str(exc).strip() or type(exc).__name__


def workflow_request_fields(stop_after_phase: Any) -> frozenset[str]:
    fields = set(WORKFLOW_REQUEST_FIELDS)
    if stop_after_phase == "phase02":
        fields.discard("analysis_runtime")
        fields.difference_update(_POST_EXECUTION_REQUEST_FIELDS)
    elif stop_after_phase == "phase03":
        fields.difference_update(_POST_EXECUTION_REQUEST_FIELDS)
    return frozenset(fields)


def _validated_workflow_request(request: Any) -> dict[str, Any]:
    if (
        type(request) is not dict
        or not _WORKFLOW_REQUIRED_REQUEST_FIELDS.issubset(request)
        or bool(set(request) - WORKFLOW_REQUEST_FIELDS)
    ):
        raise WorkflowFailure(
            "single_authority_request_shape_invalid",
            failure_type="contract",
        )
    run_id = request.get("run_id")
    run_attempt_id = request.get("run_attempt_id")
    if (
        not isinstance(run_id, str)
        or not run_id
        or run_id != run_id.strip()
        or not isinstance(run_attempt_id, str)
        or run_attempt_id != run_id
    ):
        raise WorkflowFailure(
            "single_authority_run_identity_invalid",
            failure_type="contract",
        )
    question = request["question"]
    if not isinstance(question, str) or not question or question != question.strip():
        raise WorkflowFailure(
            "single_authority_request_shape_invalid",
            failure_type="contract",
        )
    stop_after_phase = request.get("stop_after_phase")
    if stop_after_phase not in {None, "phase02", "phase03", "phase04", "phase05"}:
        raise WorkflowFailure(
            "single_authority_stop_after_phase_invalid",
            failure_type="contract",
        )
    if set(request) - workflow_request_fields(stop_after_phase):
        raise WorkflowFailure(
            "single_authority_request_shape_invalid",
            failure_type="contract",
        )
    if "recursion_limit" in request and (
        type(request["recursion_limit"]) is not int or request["recursion_limit"] < 1
    ):
        raise WorkflowFailure(
            "single_authority_recursion_limit_invalid",
            failure_type="contract",
        )
    revision_fields = set(request) & _MATERIAL_REVISION_REQUEST_FIELDS
    if revision_fields and revision_fields != _MATERIAL_REVISION_REQUEST_FIELDS:
        raise WorkflowFailure(
            "single_authority_revision_context_invalid",
            failure_type="contract",
        )
    if revision_fields:
        superseded_plan_fields = request["superseded_plan_fields"]
        if (
            any(
                not isinstance(request[field], str)
                or not request[field]
                or request[field] != request[field].strip()
                for field in (
                    "supersedes_intent_revision_id",
                    "intent_revision_reason_ref",
                    "parent_transition_id",
                )
            )
            or isinstance(superseded_plan_fields, (str, bytes))
            or not isinstance(superseded_plan_fields, Sequence)
            or not superseded_plan_fields
            or any(
                not isinstance(field, str) or not field or field != field.strip()
                for field in superseded_plan_fields
            )
            or len(superseded_plan_fields) != len(set(superseded_plan_fields))
        ):
            raise WorkflowFailure(
                "single_authority_revision_context_invalid",
                failure_type="contract",
            )
    required_runtime_fields = {
        "llm_client",
        "runtime_registry",
        "release_resolver",
    }
    if stop_after_phase != "phase02":
        required_runtime_fields.add("analysis_runtime")
    if stop_after_phase in {None, "phase04", "phase05"}:
        required_runtime_fields.update(_POST_EXECUTION_REQUEST_FIELDS)
    missing_runtime_fields = required_runtime_fields - set(request)
    if missing_runtime_fields:
        raise WorkflowFailure(
            "single_authority_runtime_dependency_invalid:"
            + ",".join(sorted(missing_runtime_fields)),
            failure_type="contract",
        )
    if (
        not callable(getattr(request["llm_client"], "invoke_json", None))
        or type(request["runtime_registry"]) is not RuntimeContractRegistry
        or not callable(
            getattr(request["release_resolver"], "resolve_dataset_release", None)
        )
        or (stop_after_phase != "phase02" and request.get("analysis_runtime") is None)
    ):
        raise WorkflowFailure(
            "single_authority_runtime_dependency_invalid",
            failure_type="contract",
        )
    if stop_after_phase in {None, "phase04", "phase05"}:
        if (
            any(
                not isinstance(request[field], str)
                or not request[field]
                or request[field] != request[field].strip()
                for field in (
                    "thread_id",
                    "owner_ref",
                    "locale",
                    "destination_ref",
                    "publication_channel",
                )
            )
            or not callable(getattr(request["authority_connection"], "execute", None))
            or not callable(request["delivery_transport"])
            or type(request.get("controlled_investigation_enabled", False)) is not bool
        ):
            raise WorkflowFailure(
                "single_authority_runtime_dependency_invalid",
                failure_type="contract",
            )
    return dict(request)


def run_single_authority_workflow(
    request: dict[str, Any],
) -> WorkflowRunResult:
    request = _validated_workflow_request(request)
    state: WorkflowState = {
        "request": request,
        "run_id": request["run_id"],
        "checkpoint_events": [],
        "llm_calls": [],
    }
    state["llm_client"] = request["llm_client"]

    try:
        output = build_single_authority_graph().invoke(
            state,
            config={"recursion_limit": request.get("recursion_limit", 80)},
        )
    except WorkflowFailure as exc:
        return WorkflowRunResult(
            status="failed",
            run_id=state["run_id"],
            failure_reason=_exception_reason(exc),
            checkpoint_events=tuple(state["checkpoint_events"]),
            llm_calls=tuple(state["llm_calls"]),
        )
    except Exception as exc:
        return WorkflowRunResult(
            status="failed",
            run_id=state["run_id"],
            failure_reason=f"langgraph_execution_failed:{_exception_reason(exc)}",
            checkpoint_events=tuple(state["checkpoint_events"]),
            llm_calls=tuple(state["llm_calls"]),
        )

    workflow_status = output.get("workflow_status")
    if workflow_status not in {
        "planned",
        "waiting_for_clarification",
        "evidence_ready",
        "authority_sealed",
        "narrative_ready",
        "completed",
        "delivery_retryable_failed",
        "delivery_permanently_failed",
        "narrative_failed",
        "publication_failed",
    }:
        return WorkflowRunResult(
            status="failed",
            run_id=str(output.get("run_id") or state["run_id"]),
            failure_reason="workflow_terminal_status_missing_or_invalid",
            checkpoint_events=tuple(output.get("checkpoint_events") or ()),
            llm_calls=tuple(output.get("llm_calls") or ()),
        )
    return WorkflowRunResult(
        status=workflow_status,
        run_id=output["run_id"],
        interaction_result=output.get("interaction_result"),
        plan_result=output.get("plan_result"),
        execution_result=output.get("execution_result"),
        factor_coverage_plan=output.get("factor_coverage_plan"),
        factor_coverage_result=output.get("factor_coverage_result"),
        investigation_branches=tuple(output.get("investigation_branches") or ()),
        investigation_synthesis=output.get("investigation_synthesis"),
        post_execution_result=output.get("post_execution_result"),
        failure_reason=str(output.get("workflow_failure_reason") or ""),
        checkpoint_events=tuple(output["checkpoint_events"]),
        llm_calls=tuple(output.get("llm_calls") or ()),
    )


def build_single_authority_graph():
    graph = StateGraph(WorkflowState)
    for node, func in (
        ("understand_business_intent", _understand_business_intent),
        ("decide_question_boundary", _decide_question_boundary),
        ("clarification_policy_gate", _clarification_policy_gate),
        ("generate_clarification", _generate_clarification),
        ("persist_clarification", _persist_clarification),
        ("compile_authoritative_plan", _compile_authoritative_plan),
        ("execute_capability_dag", _execute_capability_dag),
        ("evaluate_claim_coverage", _evaluate_claim_coverage),
        ("compile_plan_patch", _compile_plan_patch),
        ("settle_claim_authority", _settle_claim_authority),
        ("compose_claim_aware_narrative", _compose_claim_aware_narrative),
        ("deliver_publication", _deliver_publication),
    ):
        graph.add_node(node, _retrying_node(node, func))

    graph.set_entry_point("understand_business_intent")
    graph.add_edge("understand_business_intent", "decide_question_boundary")
    graph.add_edge("decide_question_boundary", "clarification_policy_gate")
    graph.add_conditional_edges(
        "clarification_policy_gate",
        _route_after_clarification_policy,
        {
            "compile": "compile_authoritative_plan",
            "ask": "generate_clarification",
        },
    )
    graph.add_edge("generate_clarification", "persist_clarification")
    graph.add_edge("persist_clarification", END)
    graph.add_conditional_edges(
        "compile_authoritative_plan",
        _route_after_authoritative_plan,
        {
            "stop": END,
            "execute": "execute_capability_dag",
        },
    )
    graph.add_conditional_edges(
        "execute_capability_dag",
        _route_after_capability_execution,
        {
            "evaluate": "evaluate_claim_coverage",
        },
    )
    graph.add_conditional_edges(
        "evaluate_claim_coverage",
        _route_after_claim_coverage,
        {
            "stop": END,
            "patch": "compile_plan_patch",
            "settle": "settle_claim_authority",
        },
    )
    graph.add_edge(
        "compile_plan_patch",
        "execute_capability_dag",
    )
    graph.add_conditional_edges(
        "settle_claim_authority",
        _route_after_authority_settlement,
        {
            "stop": END,
            "compose": "compose_claim_aware_narrative",
        },
    )
    graph.add_conditional_edges(
        "compose_claim_aware_narrative",
        _route_after_narrative_composition,
        {
            "stop": END,
            "deliver": "deliver_publication",
        },
    )
    graph.add_edge("deliver_publication", END)
    return graph.compile()


def _retrying_node(node_name, func):
    def run(state: WorkflowState) -> WorkflowState:
        started = perf_counter()
        event = _checkpoint(state, node_name, 1)
        try:
            result = func(state)
            _finish_checkpoint(event, "completed", started)
            if node_name in {
                "compile_authoritative_plan",
                "compile_plan_patch",
                "evaluate_claim_coverage",
            }:
                _refresh_planned_result(result)
            return result
        except WorkflowFailure as exc:
            event["failure_type"] = exc.failure_type
            event["reason"] = _exception_reason(exc)
            _finish_checkpoint(event, "failed", started)
            raise
        except Exception as exc:
            event["failure_type"] = "technical"
            event["reason"] = _exception_reason(exc)
            _finish_checkpoint(event, "failed", started)
            raise WorkflowFailure(
                _exception_reason(exc), failure_type="technical"
            ) from exc

    return run


def _refresh_planned_result(state: WorkflowState) -> None:
    plan_result = state.get("plan_result")
    if not isinstance(plan_result, dict):
        return
    plan_result["checkpoint_events"] = to_jsonable(state.get("checkpoint_events") or ())
    plan_result["llm_calls"] = to_jsonable(state.get("llm_calls") or ())
    state["plan_result"] = plan_result


def _understand_business_intent(state: WorkflowState) -> WorkflowState:
    request = state["request"]
    registry = request["runtime_registry"]
    authority_store = request["authority_store"]
    if not all(
        callable(getattr(authority_store, method, None))
        for method in (
            "load_accepted_transition",
            "save_intent_revision_transition",
            "load_decision_ledger",
            "latest_accepted_transition_id",
        )
    ):
        raise WorkflowFailure("single_authority_store_missing", failure_type="contract")
    question = request["question"]
    run_attempt_id = request["run_attempt_id"]
    supersedes_intent_revision_id = request.get("supersedes_intent_revision_id")
    superseded_plan_fields = (
        tuple(request["superseded_plan_fields"])
        if supersedes_intent_revision_id is not None
        else ()
    )
    parent_transition_id = (
        request["parent_transition_id"]
        if supersedes_intent_revision_id is not None
        else None
    )
    intent_revision_reason_ref = (
        request["intent_revision_reason_ref"]
        if supersedes_intent_revision_id is not None
        else "intent_binding"
    )
    source_intent_revision = None
    if supersedes_intent_revision_id is not None:
        load_intent_revision = getattr(authority_store, "load_intent_revision", None)
        if not callable(load_intent_revision):
            raise WorkflowFailure(
                "single_authority_source_intent_resolver_missing",
                failure_type="contract",
            )
        try:
            source_intent_revision = load_intent_revision(
                supersedes_intent_revision_id
            )
        except Exception as exc:
            raise WorkflowFailure(
                f"single_authority_source_intent_lookup_failed:{_exception_reason(exc)}",
                failure_type="persistence",
            ) from exc
        if (
            source_intent_revision is None
            or source_intent_revision.intent_revision_id
            != supersedes_intent_revision_id
        ):
            raise WorkflowFailure(
                "single_authority_source_intent_missing",
                failure_type="persistence",
            )
    intent_payload = _single_authority_intent_payload(
        question=question,
        registry=registry,
        source_intent_revision=source_intent_revision,
        superseded_plan_fields=superseded_plan_fields,
    )
    input_digest = canonical_digest(intent_payload)
    accepted = authority_store.load_accepted_transition(
        run_attempt_id=run_attempt_id,
        node_name="bind_intent",
        input_digest=input_digest,
    )
    if accepted is not None:
        output_payload = accepted.get("output_payload") or {}
        try:
            revision = IntentRevision.from_dict(output_payload["intent_revision"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowFailure(
                "accepted_intent_transition_invalid",
                failure_type="persistence",
            ) from exc
        if revision.run_attempt_id != run_attempt_id:
            raise WorkflowFailure(
                "accepted_intent_transition_owner_mismatch",
                failure_type="persistence",
            )
        raw_output = output_payload.get("raw_provider_output") or {}
        transition = accepted["transition"]
        _validate_provider_stage_seal(
            state,
            transition=transition,
            stage_name="bind_intent",
        )
    else:
        spec = build_prompt("single_authority_intent", intent_payload)

        def validate_provider_output(candidate: Mapping[str, Any]) -> None:
            _validated_single_authority_intent_output(
                candidate,
                run_attempt_id=run_attempt_id,
                question=question,
                registry=registry,
                prompt_version=spec.prompt_version,
                model_version=_workflow_model_ref(state),
                supersedes_intent_revision_id=supersedes_intent_revision_id,
            )

        raw_output = _invoke_llm(
            state,
            "single_authority_intent",
            intent_payload,
            output_validator=validate_provider_output,
        )
        revision = _validated_single_authority_intent_output(
            raw_output,
            run_attempt_id=run_attempt_id,
            question=question,
            registry=registry,
            prompt_version=spec.prompt_version,
            model_version=_workflow_model_ref(state),
            supersedes_intent_revision_id=supersedes_intent_revision_id,
        )
        output_payload = {
            "intent_revision": revision.to_dict(),
            "raw_provider_output": to_jsonable(raw_output),
        }
        last_audit = next(
            (
                item
                for item in reversed(state.get("llm_calls") or ())
                if isinstance(item, Mapping)
                and item.get("task") == "single_authority_intent"
            ),
            {},
        )
        inherited_ledger_position = 0
        if revision.supersedes_intent_revision_id:
            parent_ledger = authority_store.load_decision_ledger(
                revision.supersedes_intent_revision_id
            )
            inherited_ledger_position = parent_ledger.supersede_for_revision(
                revision.intent_revision_id,
                affected_plan_fields=frozenset(superseded_plan_fields),
            ).position
        transition = DurableTransition.create(
            node_name="bind_intent",
            parent_transition_id=parent_transition_id,
            run_attempt_id=run_attempt_id,
            intent_revision_id=revision.intent_revision_id,
            decision_ledger_position=inherited_ledger_position,
            input_digest=input_digest,
            output_digest=canonical_digest(output_payload),
            execution_attempt=1,
            provider_ref=str(last_audit.get("provider") or "llm_provider"),
            model_ref=str(last_audit.get("model") or _workflow_model_ref(state)),
            status="succeeded",
            acceptance_state="accepted",
            next_transition="resolve_material_decisions",
        )
        try:
            authority_store.save_intent_revision_transition(
                intent_revision=revision,
                transition=transition,
                input_payload=intent_payload,
                output_payload=output_payload,
                accepted_attempt_refs=_provider_attempt_refs(
                    state,
                    stage_name="bind_intent",
                ),
                affected_plan_fields=superseded_plan_fields,
                reason_ref=intent_revision_reason_ref,
            )
        except Exception as exc:
            raise WorkflowFailure(
                f"intent_checkpoint_persistence_failed:{_exception_reason(exc)}",
                failure_type="persistence",
            ) from exc

    ledger = authority_store.load_decision_ledger(revision.intent_revision_id)
    latest_transition_id = authority_store.latest_accepted_transition_id(run_attempt_id)
    if not latest_transition_id:
        raise WorkflowFailure(
            "single_authority_transition_head_missing",
            failure_type="persistence",
        )
    state["intent_revision"] = revision.to_dict()
    state["raw_intent_output"] = to_jsonable(raw_output)
    state["decision_ledger"] = [record.to_dict() for record in ledger.records]
    state["decision_ledger_position"] = ledger.position
    state["durable_transition_id"] = latest_transition_id
    state["durable_checkpoint"] = transition.to_dict()
    state["intent"] = _intent_revision_phase2_projection(
        revision,
        ledger=ledger,
        registry=registry,
    )
    return state


def _single_authority_intent_payload(
    *,
    question: str,
    registry: RuntimeContractRegistry,
    source_intent_revision: IntentRevision | None = None,
    superseded_plan_fields: Sequence[str] = (),
) -> dict[str, Any]:
    desired_decision_catalog = _single_authority_desired_decision_catalog(registry)
    ambiguity_slot_catalog = _single_authority_ambiguity_slot_catalog()
    payload = {
        "original_user_text": question,
        "goal_catalog": [
            _intent_goal_catalog_item(
                goal_id,
                registry.analysis_goal_obligation(goal_id),
            )
            for goal_id in registry.analysis_goal_ids
        ],
        "metric_catalog": [
            {
                "metric_id": metric_id,
                "business_labels": list(registry.metric_business_labels(metric_id)),
            }
            for metric_id in registry.metric_ids
        ],
        "analysis_axis_catalog": [
            _intent_axis_catalog_item(
                axis_id,
                registry.analysis_axis(axis_id),
            )
            for axis_id in registry.analysis_axis_ids
        ],
        "scope_type_catalog": list(registry.public_scope_types),
        "time_spec_contract": {
            "allowed_kinds": ["date", "date_range", "relative", "period", "custom"],
            "business_timezone": registry.business_timezone,
            "variants": {
                "date": {"kind": "date", "target": "YYYY-MM-DD"},
                "date_range": {
                    "kind": "date_range",
                    "start": "YYYY-MM-DD",
                    "end": "YYYY-MM-DD",
                },
                "relative": {"kind": "relative", "reference": "catalog value"},
                "period": {"kind": "period", "period_ref": "canonical period"},
                "custom": {"kind": "custom", "expression": "typed expression"},
            },
        },
        "comparison_spec_contract": {
            "authority_rule": (
                "Copy an explicit user comparison into comparison_spec. Use a "
                "decision_slot when a material comparison reference or its "
                "SQL-affecting interpretation remains unresolved."
            ),
            "variants": {
                "none": {"kind": "none"},
                "decision_slot": {
                    "kind": "decision_slot",
                    "slot_id": "ambiguity_slot_catalog.slot_id",
                },
                "fixed_window": {
                    "kind": "fixed_window",
                    "baseline_class": [
                        "prior_period",
                        "same_period_last_year",
                        "custom_control_window",
                    ],
                    "baseline_start": "YYYY-MM-DD",
                    "baseline_end": "YYYY-MM-DD",
                    "aggregation": [
                        "sum_of_complete_days",
                        "mean_of_complete_days",
                    ],
                },
                "calendar_partition": {
                    "kind": "calendar_partition",
                    "baseline_class": (
                        "calendar_partition_contracts[partition_field]."
                        "baseline_classes"
                    ),
                    "period_grain": (
                        "calendar_partition_contracts[partition_field]."
                        "period_grain"
                    ),
                    "partition_field": [
                        "quarter_of_year",
                        "month_of_year",
                        "month_phase",
                        "iso_weekday",
                    ],
                    "target_members": "non-empty contract member set",
                    "baseline_members": "non-empty disjoint contract member set",
                    "aggregation": [
                        "sum_of_complete_days",
                        "mean_of_complete_days",
                    ],
                    "member_definitions": (
                        "required only for month_phase: three explicit contiguous "
                        "{member, day_start, day_end} ranges covering days 1..31"
                    ),
                },
                "event_relative_window": {
                    "kind": "event_relative_window",
                    "event_ref": "explicit user or accepted decision event ref",
                    "target_start": "YYYY-MM-DD",
                    "target_end": "YYYY-MM-DD",
                    "baseline_start": "YYYY-MM-DD",
                    "baseline_end": "YYYY-MM-DD",
                    "aggregation": [
                        "sum_of_complete_days",
                        "mean_of_complete_days",
                    ],
                },
            },
            "calendar_partition_contracts": calendar_partition_llm_contracts(),
            "target_authority_by_kind": {
                "fixed_window": "time_spec",
                "calendar_partition": "time_spec.date_range evaluation window",
                "event_relative_window": (
                    "event bounds, required to equal time_spec when time_spec is physical"
                ),
            },
        },
        "ambiguity_value_catalog": {
            "baseline": list(baseline_llm_semantics()),
            "comparison_window": [
                {
                    "id": value_ref,
                    "typed_value_kind": "fixed_window_or_calendar_partition",
                }
                for value_ref in COMPARISON_WINDOW_VALUE_REFS
            ],
            "comparison_interpretation": [
                {
                    "id": value_ref,
                    "typed_value_kind": (
                        "complete_fixed_window_or_calendar_partition"
                    ),
                }
                for value_ref in _COMPARISON_INTERPRETATION_VALUE_REFS
            ],
        },
        "ambiguity_slot_catalog": ambiguity_slot_catalog,
        "ambiguity_slot_output_contract": {
            "required_keys": [
                "slot_id",
                "slot_kind",
                "materiality",
                "status",
                "question",
                "allowed_value_refs",
            ],
            "question_key": "question",
            "forbidden_keys": ["business_question"],
        },
        "desired_decision_catalog": desired_decision_catalog,
        "source_span_contract": {
            "required": [
                {
                    "field": "original_user_text",
                    "start": 0,
                    "end": len(question),
                    "text": question,
                }
            ]
        },
    }
    if source_intent_revision is not None:
        if not isinstance(source_intent_revision, IntentRevision):
            raise ValueError("source_intent_revision_invalid")
        payload["revision_context"] = {
            "source_original_user_text": (
                source_intent_revision.original_user_text
            ),
            "source_business_summary": source_intent_revision.business_summary,
            "source_intent_binding": {
                "goal_bindings": canonical_value(
                    source_intent_revision.goal_bindings
                ),
                "target_metric_refs": canonical_value(
                    source_intent_revision.target_metric_refs
                ),
                "scope": canonical_value(source_intent_revision.scope),
                "time_spec": canonical_value(source_intent_revision.time_spec),
                "comparison_spec": canonical_value(
                    source_intent_revision.comparison_spec
                ),
                "direction_premise": source_intent_revision.direction_premise,
                "requested_analysis_axes": canonical_value(
                    source_intent_revision.requested_analysis_axes
                ),
                "requested_factor_refs": canonical_value(
                    source_intent_revision.requested_factor_refs
                ),
                "desired_decisions": canonical_value(
                    source_intent_revision.desired_decisions
                ),
                "ambiguity_slots": canonical_value(
                    source_intent_revision.ambiguity_slots
                ),
            },
            "superseded_plan_fields": list(superseded_plan_fields),
        }
    return payload


def _intent_goal_catalog_item(
    goal_id: str,
    obligation: Mapping[str, Any],
) -> dict[str, Any]:
    """Project only business intent fields; execution authority stays in the registry."""

    return {
        "goal_id": goal_id,
        "business_name": obligation["business_name"],
        "semantics": obligation["semantics"],
        "question_family_ref": obligation["question_family_ref"],
        "target_metric_refs": list(obligation["target_metric_refs"]),
        "required_outcomes": list(obligation["required_outcomes"]),
        "analysis_axes": [
            {
                "axis_id": item["axis_id"],
                "role": item["role"],
            }
            for item in obligation["analysis_axes"]
        ],
    }


def _intent_axis_catalog_item(
    axis_id: str,
    axis: Mapping[str, Any],
) -> dict[str, Any]:
    """Expose business selection semantics without duplicating execution routing."""

    return {
        "axis_id": axis_id,
        "business_name": axis["business_name"],
        "semantics": axis["semantics"],
        "axis_kind": axis["axis_kind"],
        "target_metric_refs": list(axis["target_metric_refs"]),
        "metric_refs": list(axis["metric_refs"]),
        "dimension_refs": list(axis["dimension_refs"]),
        "context_source_refs": list(axis["context_source_refs"]),
    }


def _single_authority_desired_decision_catalog(
    registry: RuntimeContractRegistry,
) -> list[dict[str, str]]:
    catalog: list[dict[str, str]] = []
    for goal_id in registry.analysis_goal_ids:
        obligation = registry.analysis_goal_obligation(goal_id)
        for decision_kind in obligation.get("required_outcomes") or ():
            for target_ref in obligation.get("target_metric_refs") or ():
                item = {
                    "goal_id": goal_id,
                    "decision_kind": str(decision_kind),
                    "target_ref": str(target_ref),
                }
                if item not in catalog:
                    catalog.append(item)
    return catalog


def _single_authority_ambiguity_slot_catalog() -> list[dict[str, Any]]:
    return [
        {
            "slot_id": "comparison_baseline",
            "slot_kind": "baseline",
            "materiality": "material",
            "allowed_value_refs": list(CANONICAL_BASELINE_IDS),
            "time_spec_kinds": ["date"],
        },
        {
            "slot_id": "comparison_window",
            "slot_kind": "comparison_window",
            "materiality": "material",
            "allowed_value_refs": list(COMPARISON_WINDOW_VALUE_REFS),
            "time_spec_kinds": ["date_range"],
        },
        {
            "slot_id": "comparison_interpretation",
            "slot_kind": "comparison_interpretation",
            "materiality": "material",
            "allowed_value_refs": list(
                _COMPARISON_INTERPRETATION_VALUE_REFS
            ),
            "time_spec_kinds": ["date_range"],
        },
        {
            "slot_id": "month_phase_definition",
            "slot_kind": "month_phase_definition",
            "materiality": "material",
            "allowed_value_refs": list(MONTH_PHASE_DEFINITION_VALUE_REFS),
            "time_spec_kinds": ["date_range"],
        },
        {
            "slot_id": "phase_aggregation",
            "slot_kind": "phase_aggregation",
            "materiality": "material",
            "allowed_value_refs": list(PHASE_AGGREGATION_VALUE_REFS),
            "time_spec_kinds": ["date_range"],
        },
        {
            "slot_id": "event_relative_window",
            "slot_kind": "event_relative_window",
            "materiality": "material",
            "allowed_value_refs": [],
            "time_spec_kinds": [
                "date",
                "date_range",
                "relative",
                "period",
                "custom",
            ],
        },
    ]


def _validated_single_authority_intent_output(
    output: Mapping[str, Any],
    *,
    run_attempt_id: str,
    question: str,
    registry: RuntimeContractRegistry,
    prompt_version: str,
    model_version: str,
    supersedes_intent_revision_id: str | None,
) -> IntentRevision:
    if not isinstance(output, Mapping) or set(output) != {
        "intent_binding",
        "business_summary",
        "status_message",
    }:
        raise LLMOutputError("single_authority_intent_output_shape_invalid")
    if any(
        not isinstance(output[field], str) or not output[field].strip()
        for field in ("business_summary", "status_message")
    ):
        raise LLMOutputError("single_authority_intent_narrative_invalid")
    normalized_binding = _normalize_provider_intent_binding(output["intent_binding"])
    try:
        return IntentRevision.from_provider_binding(
            normalized_binding,
            run_attempt_id=run_attempt_id,
            supersedes_intent_revision_id=supersedes_intent_revision_id,
            original_user_text=question,
            business_summary=output["business_summary"].strip(),
            schema_version="intent-revision.v3",
            prompt_version=prompt_version,
            model_version=model_version,
            known_goal_ids=set(registry.analysis_goal_ids),
            known_metric_ids=set(registry.metric_ids),
            known_analysis_axis_ids=set(registry.analysis_axis_ids),
            known_scope_types=set(registry.public_scope_types),
            known_filter_fields=set(registry.all_customer_safe_filter_fields),
            known_ambiguity_value_refs={
                *CANONICAL_BASELINE_IDS,
                *COMPARISON_WINDOW_VALUE_REFS,
                *_COMPARISON_INTERPRETATION_VALUE_REFS,
                *MONTH_PHASE_DEFINITION_VALUE_REFS,
                *PHASE_AGGREGATION_VALUE_REFS,
            },
            known_desired_decision_kinds={
                item["decision_kind"]
                for item in _single_authority_desired_decision_catalog(registry)
            },
            known_desired_decision_target_refs={
                item["target_ref"]
                for item in _single_authority_desired_decision_catalog(registry)
            },
            known_ambiguity_slots={
                item["slot_id"]: item
                for item in _single_authority_ambiguity_slot_catalog()
            },
        )
    except (TypeError, ValueError) as exc:
        raise LLMOutputError(_exception_reason(exc)) from exc


def _normalize_provider_intent_binding(value: Any) -> Any:
    """Normalize only deterministic duplicates before strict authority validation."""

    if not isinstance(value, Mapping):
        return value
    normalized = dict(value)

    def scalar_enum(candidate: Any) -> Any:
        if (
            isinstance(candidate, Sequence)
            and not isinstance(candidate, (str, bytes))
            and len(candidate) == 1
            and isinstance(candidate[0], str)
        ):
            return candidate[0]
        return candidate

    raw_time_spec = value.get("time_spec")
    if isinstance(raw_time_spec, Mapping):
        normalized["time_spec"] = {
            **dict(raw_time_spec),
            "kind": scalar_enum(raw_time_spec.get("kind")),
        }
    raw_comparison_spec = value.get("comparison_spec")
    if isinstance(raw_comparison_spec, Mapping):
        normalized["comparison_spec"] = {
            **dict(raw_comparison_spec),
            **{
                field: scalar_enum(raw_comparison_spec.get(field))
                for field in (
                    "kind",
                    "baseline_class",
                    "aggregation",
                    "period_grain",
                    "partition_field",
                )
                if field in raw_comparison_spec
            },
        }
    target_metric_refs = value.get("target_metric_refs")
    requested_factor_refs = value.get("requested_factor_refs")
    if (
        isinstance(target_metric_refs, Sequence)
        and not isinstance(target_metric_refs, (str, bytes))
        and all(isinstance(item, str) for item in target_metric_refs)
        and isinstance(requested_factor_refs, Sequence)
        and not isinstance(requested_factor_refs, (str, bytes))
        and all(isinstance(item, str) for item in requested_factor_refs)
    ):
        target_refs = set(target_metric_refs)
        normalized["requested_factor_refs"] = [
            item for item in requested_factor_refs if item not in target_refs
        ]
    time_spec = normalized.get("time_spec")
    comparison_spec = normalized.get("comparison_spec")
    if (
        not isinstance(time_spec, Mapping)
        or not isinstance(comparison_spec, Mapping)
        or comparison_spec.get("kind") != "fixed_window"
        or not {"target_start", "target_end"}.issubset(comparison_spec)
    ):
        return normalized
    try:
        bounds = target_bounds(time_spec)
    except TemporalComparisonContractError:
        return normalized
    if bounds != (
        comparison_spec.get("target_start"),
        comparison_spec.get("target_end"),
    ):
        return normalized
    normalized_comparison = dict(comparison_spec)
    normalized_comparison.pop("target_start")
    normalized_comparison.pop("target_end")
    normalized["comparison_spec"] = normalized_comparison
    return normalized


def _workflow_model_ref(state: Mapping[str, Any]) -> str:
    client = state.get("llm_client")
    return str(
        getattr(client, "model", "")
        or getattr(client, "critical_model", "")
        or "configured-model"
    )


def _intent_revision_phase2_projection(
    revision: IntentRevision,
    *,
    ledger: DecisionLedger,
    registry: RuntimeContractRegistry,
) -> dict[str, Any]:
    primary_goal = next(
        binding["goal_id"]
        for binding in revision.goal_bindings
        if binding["role"] == "primary"
    )
    question_family = registry.analysis_goal_question_family_ref(primary_goal)
    if question_family not in set(registry.launch_question_family_ids):
        raise WorkflowFailure(
            f"intent_goal_question_family_contract_missing:{primary_goal}",
            failure_type="contract",
        )
    try:
        temporal_authority = resolve_effective_comparison(
            time_spec=revision.time_spec,
            comparison_spec=revision.comparison_spec,
            decision_ledger=ledger,
            require_physical_baseline=False,
        )
    except TemporalComparisonContractError as exc:
        raise WorkflowFailure(
            f"intent_temporal_authority_invalid:{_exception_reason(exc)}",
            failure_type="contract",
        ) from exc
    baseline_refs = list(
        temporal_authority.baseline_ids or temporal_authority.baseline_window_refs
    )
    time_window: Any = revision.time_spec
    if revision.time_spec.get("kind") == "date":
        time_window = revision.time_spec.get("target")
    plan = registry.compile_goal_analysis_plan(
        goal_bindings=revision.goal_bindings,
        target_metric=revision.target_metric_refs[0],
        explicit_focus={
            "component_ids": list(revision.requested_factor_refs),
            "dimension_ids": [],
            "context_source_ids": [],
        },
    )
    return {
        "question_family": question_family,
        "primary_question_family": question_family,
        "question_families": list(plan["question_family_refs"]),
        "secondary_question_families": list(
            plan["merged_question_family_refs"]
        ),
        "target_metric": revision.target_metric_refs[0],
        "pattern_family": "custom_baseline",
        "pattern_params": {},
        "scope": canonical_value(revision.scope),
        "time_window": canonical_value(time_window),
        "target_claim": "comparative_change",
        "baseline_candidates": baseline_refs,
        "baseline_binding": {
            "confirmed": bool(baseline_refs),
            "decision_refs": list(temporal_authority.decision_refs),
        },
        "temporal_authority": temporal_authority.to_dict(),
        "sub_intents": [],
        "ambiguous_slots": [canonical_value(slot) for slot in revision.ambiguity_slots],
        "answer_contract": {},
        "question": revision.original_user_text,
        "goal_bindings": [canonical_value(item) for item in revision.goal_bindings],
        "explicit_focus": plan["explicit_focus"],
        "analysis_plan": plan,
        "analysis_axis_ids": list(revision.requested_analysis_axes),
        "required_outcomes": list(plan["required_outcomes"]),
        "publishable_claim_types": list(
            dict.fromkeys(
                claim_type
                for claim_types in plan["outcome_claim_types"].values()
                for claim_type in claim_types
            )
        ),
        "component_ids": list(plan["explicit_focus"]["component_ids"]),
        "dimension_ids": [],
        "association_metric_ids": [],
        "context_sources": [],
    }


def _empty_business_context_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value:
        return True
    if isinstance(value, (dict, list, tuple, set)) and not value:
        return True
    return False


def _decide_question_boundary(state: WorkflowState) -> WorkflowState:
    revision = IntentRevision.from_dict(state["intent_revision"])
    ledger = DecisionLedger(
        records=tuple(
            DecisionRecord.from_dict(record)
            for record in state.get("decision_ledger") or ()
        )
    )
    unresolved = [
        canonical_value(slot)
        for slot in revision.ambiguity_slots
        if slot.get("status") == "unresolved"
        and ledger.active_for_slot(str(slot.get("slot_id") or "")) is None
    ]
    material = [slot for slot in unresolved if slot.get("materiality") == "material"]
    if material:
        state["boundary_decision"] = {
            "boundary_status": "needs_question",
            "ambiguity_slots": material,
            "remaining_ambiguity_slot_ids": [str(slot["slot_id"]) for slot in material],
            "decision_summary": "当前仍有会改变分析计划的业务选择需要确认。",
        }
    else:
        state["boundary_decision"] = {
            "boundary_status": "clear",
            "ambiguity_slots": [],
            "remaining_ambiguity_slot_ids": [],
            "decision_summary": "当前 material 决定已绑定，可以进入下一节点。",
        }
    return state


def _clarification_policy_gate(state: WorkflowState) -> WorkflowState:
    if not state.get("intent_revision"):
        raise WorkflowFailure(
            "single_authority_intent_revision_missing",
            failure_type="contract",
        )
    status = state["boundary_decision"].get("boundary_status")
    if status not in {"clear", "needs_question"}:
        raise WorkflowFailure(
            "single_authority_boundary_status_invalid",
            failure_type="contract",
        )
    state["clarification_outcome"] = {
        "status": "pending" if status == "needs_question" else "resolved",
        "boundary_status": status,
        "ambiguity_slots": canonical_value(
            state["boundary_decision"].get("ambiguity_slots") or []
        ),
    }
    _current_event(state)["route"] = status
    return state


def _single_authority_clarification_allowed_values(
    slot: Mapping[str, Any],
) -> list[dict[str, Any]]:
    allowed_refs = tuple(slot.get("allowed_value_refs") or ())
    slot_kind = str(slot.get("slot_kind") or "")
    if slot_kind == "baseline":
        catalog = {str(item["id"]): dict(item) for item in baseline_llm_semantics()}
        if any(value_ref not in catalog for value_ref in allowed_refs):
            raise WorkflowFailure(
                "single_authority_clarification_value_ref_unknown",
                failure_type="contract",
            )
        return [catalog[value_ref] for value_ref in allowed_refs]
    if slot_kind == "comparison_window":
        if any(
            value_ref not in COMPARISON_WINDOW_VALUE_REFS for value_ref in allowed_refs
        ):
            raise WorkflowFailure(
                "single_authority_clarification_value_ref_unknown",
                failure_type="contract",
            )
        kinds_by_ref = {
            "prior_period": ["fixed_window", "calendar_partition"],
            "same_period_last_year": ["fixed_window"],
            "same_month_phase": ["calendar_partition"],
            "custom_control_window": ["fixed_window", "calendar_partition"],
        }
        return [
            {
                "id": value_ref,
                "admissible_typed_value_kinds": kinds_by_ref[value_ref],
            }
            for value_ref in allowed_refs
        ]
    if slot_kind == "comparison_interpretation":
        if tuple(allowed_refs) != _COMPARISON_INTERPRETATION_VALUE_REFS:
            raise WorkflowFailure(
                "single_authority_clarification_value_ref_unknown",
                failure_type="contract",
            )
        return [
            {
                "id": value_ref,
                "admissible_typed_value_kinds": [
                    "fixed_window",
                    "calendar_partition",
                ],
                "semantics": (
                    "one complete business interpretation whose SQL-affecting "
                    "dates, member ranges, and aggregation are explicit"
                ),
            }
            for value_ref in allowed_refs
        ]
    if slot_kind == "month_phase_definition":
        if tuple(allowed_refs) != MONTH_PHASE_DEFINITION_VALUE_REFS:
            raise WorkflowFailure(
                "single_authority_clarification_value_ref_unknown",
                failure_type="contract",
            )
        return [
            {
                "id": value_ref,
                "typed_value_kind": "month_phase_definition",
                "semantics": (
                    "one explicit start, mid, end day-of-month partition "
                    "covering days 1 through 31"
                ),
            }
            for value_ref in allowed_refs
        ]
    if slot_kind == "phase_aggregation":
        if tuple(allowed_refs) != PHASE_AGGREGATION_VALUE_REFS:
            raise WorkflowFailure(
                "single_authority_clarification_value_ref_unknown",
                failure_type="contract",
            )
        semantics = {
            "sum_of_complete_days": "compare each phase's total amount",
            "mean_of_complete_days": "compare the daily mean inside each phase",
        }
        return [
            {
                "id": value_ref,
                "typed_value_kind": "phase_aggregation",
                "semantics": semantics[value_ref],
            }
            for value_ref in allowed_refs
        ]
    if slot_kind == "event_relative_window" and not allowed_refs:
        return []
    raise WorkflowFailure(
        "single_authority_clarification_slot_kind_invalid",
        failure_type="contract",
    )


def _single_authority_decision_option_record(
    *,
    slot: Mapping[str, Any],
    time_spec: Mapping[str, Any],
    option: Mapping[str, Any],
) -> dict[str, Any]:
    slot_id = str(slot["slot_id"])
    typed_value = (
        option["typed_value"]
        if slot.get("slot_kind")
        in {
            "comparison_window",
            "comparison_interpretation",
            "month_phase_definition",
            "phase_aggregation",
        }
        else {"baseline_id": option["value_ref"]}
    )
    return {
        "slot_id": slot_id,
        "option_id": temporal_decision_option_id(
            slot_id=slot_id,
            value=typed_value,
            time_spec=time_spec,
        ),
        "typed_value": canonical_value(typed_value),
        "display_label": option["label"],
        "display_description": option["description"],
        "recommended": option["recommended"],
    }


def _single_authority_clarification_slot_contract(
    *,
    slot: Mapping[str, Any],
    comparison_spec: Mapping[str, Any],
) -> dict[str, Any]:
    allowed_refs = tuple(slot.get("allowed_value_refs") or ())
    allowed_values = _single_authority_clarification_allowed_values(slot)
    slot_kind = str(slot.get("slot_kind") or "")
    recommended_value_ref = ""
    required_recommended_typed_value: Mapping[str, Any] | None = None
    required_recommended_label = ""
    if slot_kind == "baseline" and "previous_day" in allowed_refs:
        baseline_catalog = {
            str(item["id"]): dict(item) for item in baseline_llm_semantics()
        }
        recommended_value_ref = "previous_day"
        required_recommended_label = (
            f"跟{baseline_catalog[recommended_value_ref]['label']}比较（推荐）"
        )
    elif slot_kind == "month_phase_definition":
        recommended_value_ref = "definition_1"
        required_recommended_typed_value = {
            "value_ref": recommended_value_ref,
            "member_definitions": canonical_value(
                comparison_spec.get("member_definitions") or ()
            ),
        }
    elif slot_kind == "phase_aggregation":
        recommended_value_ref = str(comparison_spec.get("aggregation") or "")
        required_recommended_typed_value = {
            "aggregation": recommended_value_ref,
        }

    if slot_kind in {"comparison_window", "comparison_interpretation"}:
        option_output_contract = {
            "required_keys": [
                "value_ref",
                "typed_value",
                "label",
                "description",
                "recommended",
            ],
            "typed_value_authority": "complete_comparison_spec",
            "typed_value_variants": {
                "fixed_window": {
                    "kind": "fixed_window",
                    "baseline_class": "value_ref",
                    "baseline_start": "YYYY-MM-DD",
                    "baseline_end": "YYYY-MM-DD",
                    "aggregation": list(PHASE_AGGREGATION_VALUE_REFS),
                },
                "calendar_partition": {
                    "kind": "calendar_partition",
                    "baseline_class": "contract value",
                    "period_grain": "contract value",
                    "partition_field": [
                        "quarter_of_year",
                        "month_of_year",
                        "month_phase",
                        "iso_weekday",
                    ],
                    "target_members": "non-empty contract member set",
                    "baseline_members": "non-empty disjoint contract member set",
                    "aggregation": list(PHASE_AGGREGATION_VALUE_REFS),
                    "member_definitions": (
                        "required for month_phase: three contiguous ranges "
                        "covering days 1 through 31"
                    ),
                },
            },
            "calendar_partition_contracts": calendar_partition_llm_contracts(),
        }
    elif slot_kind == "month_phase_definition":
        option_output_contract = {
            "required_keys": [
                "value_ref",
                "typed_value",
                "label",
                "description",
                "recommended",
            ],
            "typed_value_authority": "month_phase_definition",
            "typed_value_shape": {
                "value_ref": "must equal option value_ref",
                "member_definitions": (
                    "three contiguous {member, day_start, day_end} ranges "
                    "covering days 1 through 31"
                ),
            },
        }
    elif slot_kind == "phase_aggregation":
        option_output_contract = {
            "required_keys": [
                "value_ref",
                "typed_value",
                "label",
                "description",
                "recommended",
            ],
            "typed_value_authority": "phase_aggregation",
            "typed_value_shape": {
                "aggregation": list(PHASE_AGGREGATION_VALUE_REFS),
            },
        }
    else:
        option_output_contract = {
            "required_keys": [
                "value_ref",
                "label",
                "description",
                "recommended",
            ],
            "typed_value_authority": "runtime_catalog_mapping",
        }
    return {
        "slot": canonical_value(slot),
        "allowed_values": allowed_values,
        "recommended_value_ref": recommended_value_ref,
        "required_recommended_display_label": required_recommended_label,
        "required_recommended_typed_value": canonical_value(
            required_recommended_typed_value
        ),
        "option_output_contract": option_output_contract,
    }


def _generate_clarification(state: WorkflowState) -> WorkflowState:
    if state.get("intent_revision"):
        slots = state.get("boundary_decision", {}).get("ambiguity_slots") or []
        if (
            not isinstance(slots, list)
            or not slots
            or any(not isinstance(slot, Mapping) for slot in slots)
        ):
            raise WorkflowFailure(
                "single_authority_clarification_slot_missing",
                failure_type="contract",
            )
        comparison_spec = state["intent_revision"]["comparison_spec"]
        slot_contracts = [
            _single_authority_clarification_slot_contract(
                slot=slot,
                comparison_spec=comparison_spec,
            )
            for slot in slots
        ]
        clarification_payload = {
            "intent_revision_ref": state["intent_revision"]["intent_revision_id"],
            "goal_bindings": state["intent_revision"]["goal_bindings"],
            "target_metric_refs": state["intent_revision"]["target_metric_refs"],
            "time_spec": state["intent_revision"]["time_spec"],
            "comparison_spec": comparison_spec,
            "clarification_slots": slot_contracts,
            "question_output_contract": {
                "required_keys": [
                    "slot_id",
                    "question",
                    "options",
                    "recommendation_reason",
                ],
                "one_question_per_slot": True,
            },
        }
        authority_store = state["request"].get("authority_store")
        input_digest = canonical_digest(clarification_payload)
        accepted = authority_store.load_accepted_transition(
            run_attempt_id=state["run_id"],
            node_name="generate_clarification",
            input_digest=input_digest,
        )
        if accepted is not None:
            output_payload = accepted.get("output_payload") or {}
            decision_options = output_payload.get("decision_options")
            clarification_outcome = output_payload.get("clarification_outcome")
            if not isinstance(decision_options, list) or not isinstance(
                clarification_outcome, Mapping
            ):
                raise WorkflowFailure(
                    "accepted_clarification_transition_invalid",
                    failure_type="persistence",
                )
            state["decision_options"] = canonical_value(decision_options)
            state["clarification_outcome"] = canonical_value(clarification_outcome)
            state["raw_clarification_output"] = canonical_value(
                output_payload.get("raw_provider_output") or {}
            )
            transition = accepted["transition"]
            _validate_provider_stage_seal(
                state,
                transition=transition,
                stage_name="generate_clarification",
            )
            state["durable_transition_id"] = transition.transition_id
            state["durable_checkpoint"] = transition.to_dict()
            return state
        output = _invoke_llm(
            state,
            "single_authority_clarification",
            clarification_payload,
            output_projector=lambda candidate: (
                _project_single_authority_clarification_output(
                    candidate,
                    slot_contracts=slot_contracts,
                )
            ),
            output_validator=lambda candidate: (
                _validate_single_authority_clarification_batch_output(
                    candidate,
                    slot_contracts=slot_contracts,
                    time_spec=state["intent_revision"]["time_spec"],
                )
            ),
        )
        questions = _validate_single_authority_clarification_batch_output(
            output,
            slot_contracts=slot_contracts,
            time_spec=state["intent_revision"]["time_spec"],
        )
        public_language_issues = _clarification_public_language_issues(
            output=output,
            slot_contracts=slot_contracts,
        )
        if public_language_issues:
            authority_store.add_audit_event(
                "llm_public_language_quality_recorded",
                thread_id=str(state["request"].get("thread_id") or ""),
                topic_id=str(state["request"].get("topic_id") or ""),
                run_id=state["run_id"],
                ref=state["intent_revision"]["intent_revision_id"],
                payload={
                    "task": "single_authority_clarification",
                    "policy": "record_only",
                    "issues": public_language_issues,
                },
            )
        option_records = [
            _single_authority_decision_option_record(
                slot=question["slot"],
                time_spec=state["intent_revision"]["time_spec"],
                option=option,
            )
            for question in questions
            for option in question["options"]
        ]
        escape_option = {
            "option_id": "tell_agent_differently",
            "label": CLARIFICATION_ESCAPE_OPTION,
            "description": "自己说明当前业务选择，或明确修改分析目标。",
            "recommended": False,
        }
        state["decision_options"] = option_records
        state["clarification_outcome"] = {
            "status": "question_tool_opened",
            "boundary_status": "needs_question",
            "questions": [
                {
                    "slot_id": str(question["slot"]["slot_id"]),
                    "slot_kind": str(question["slot"]["slot_kind"]),
                    "question": str(question["question"]),
                    "options": [
                        {
                            "option_id": record["option_id"],
                            "label": record["display_label"],
                            "description": record["display_description"],
                            "recommended": record["recommended"],
                            "typed_value": record["typed_value"],
                        }
                        for record in option_records
                        if record["slot_id"] == question["slot"]["slot_id"]
                    ]
                    + [escape_option],
                    "recommendation_reason": str(
                        question["recommendation_reason"]
                    ),
                }
                for question in questions
            ],
            "status_message": str(output["status_message"]),
        }
        output_payload = {
            "decision_options": canonical_value(option_records),
            "clarification_outcome": canonical_value(state["clarification_outcome"]),
            "public_language_issues": canonical_value(public_language_issues),
            "raw_provider_output": canonical_value(
                state.get("raw_provider_outputs", {}).get(
                    "generate_clarification",
                    output,
                )
            ),
        }
        last_audit = next(
            (
                item
                for item in reversed(state.get("llm_calls") or ())
                if isinstance(item, Mapping)
                and item.get("task") == "single_authority_clarification"
            ),
            {},
        )
        transition = DurableTransition.create(
            node_name="generate_clarification",
            parent_transition_id=str(state["durable_transition_id"]),
            run_attempt_id=state["run_id"],
            intent_revision_id=state["intent_revision"]["intent_revision_id"],
            decision_ledger_position=int(state.get("decision_ledger_position") or 0),
            input_digest=input_digest,
            output_digest=canonical_digest(output_payload),
            execution_attempt=1,
            provider_ref=str(last_audit.get("provider") or "llm_provider"),
            model_ref=str(last_audit.get("model") or _workflow_model_ref(state)),
            status="succeeded",
            acceptance_state="accepted",
            next_transition="persist_waiting_for_decision",
        )
        try:
            authority_store.save_decision_options_transition(
                intent_revision_id=state["intent_revision"]["intent_revision_id"],
                options=option_records,
                transition=transition,
                input_payload=clarification_payload,
                output_payload=output_payload,
                accepted_attempt_refs=_provider_attempt_refs(
                    state,
                    stage_name="generate_clarification",
                ),
            )
        except Exception as exc:
            raise WorkflowFailure(
                f"clarification_checkpoint_persistence_failed:{_exception_reason(exc)}",
                failure_type="persistence",
            ) from exc
        state["raw_clarification_output"] = canonical_value(
            output_payload["raw_provider_output"]
        )
        state["durable_transition_id"] = transition.transition_id
        state["durable_checkpoint"] = transition.to_dict()
        return state
    raise WorkflowFailure("single_authority_intent_missing", failure_type="contract")


def _validate_single_authority_clarification_batch_output(
    output: Mapping[str, Any],
    *,
    slot_contracts: Sequence[Mapping[str, Any]],
    time_spec: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(output, Mapping) or set(output) != {
        "questions",
        "status_message",
    }:
        raise LLMOutputError("single_authority_clarification_shape_invalid")
    status_message = output.get("status_message")
    raw_questions = output.get("questions")
    if (
        not isinstance(status_message, str)
        or not status_message.strip()
        or not isinstance(raw_questions, list)
        or len(raw_questions) != len(slot_contracts)
        or any(not isinstance(item, Mapping) for item in raw_questions)
    ):
        raise LLMOutputError("single_authority_clarification_questions_invalid")
    normalized: list[dict[str, Any]] = []
    for raw_question, contract in zip(
        raw_questions,
        slot_contracts,
        strict=True,
    ):
        if set(raw_question) != {
            "slot_id",
            "question",
            "options",
            "recommendation_reason",
        }:
            raise LLMOutputError(
                "single_authority_clarification_question_shape_invalid"
            )
        slot = contract.get("slot")
        if (
            not isinstance(slot, Mapping)
            or raw_question.get("slot_id") != slot.get("slot_id")
        ):
            raise LLMOutputError(
                "single_authority_clarification_question_slot_invalid"
            )
        options = _validate_single_authority_clarification_output(
            {
                "question": raw_question.get("question"),
                "options": raw_question.get("options"),
                "recommendation_reason": raw_question.get(
                    "recommendation_reason"
                ),
                "status_message": status_message,
            },
            slot=slot,
            time_spec=time_spec,
            required_recommended_value_ref=str(
                contract.get("recommended_value_ref") or ""
            ),
            required_recommended_label=str(
                contract.get("required_recommended_display_label") or ""
            ),
            required_recommended_typed_value=contract.get(
                "required_recommended_typed_value"
            ),
        )
        normalized.append(
            {
                "slot": canonical_value(slot),
                "question": str(raw_question["question"]).strip(),
                "options": options,
                "recommendation_reason": str(
                    raw_question["recommendation_reason"]
                ).strip(),
            }
        )
    return normalized


def _project_single_authority_clarification_output(
    output: Mapping[str, Any],
    *,
    slot_contracts: Sequence[Mapping[str, Any]],
) -> ContractProjection:
    mutations: list[Mapping[str, str]] = []
    projected = project_mapping_fields(
        output,
        allowed_fields=("questions", "status_message"),
        path="",
        mutations=mutations,
    )
    if not isinstance(projected, dict):
        return ContractProjection.create(output=output)
    raw_questions = projected.get("questions")
    if not isinstance(raw_questions, list):
        return ContractProjection.create(output=projected, mutations=mutations)
    projected_questions: list[Any] = []
    for index, raw_question in enumerate(raw_questions):
        question_path = f"questions[{index}]"
        question = project_mapping_fields(
            raw_question,
            allowed_fields=(
                "slot_id",
                "question",
                "options",
                "recommendation_reason",
            ),
            path=question_path,
            mutations=mutations,
        )
        if not isinstance(question, dict):
            projected_questions.append(question)
            continue
        contract = slot_contracts[index] if index < len(slot_contracts) else {}
        option_contract = contract.get("option_output_contract")
        required_option_fields = (
            option_contract.get("required_keys")
            if isinstance(option_contract, Mapping)
            else None
        )
        raw_options = question.get("options")
        if not isinstance(raw_options, list) or not isinstance(
            required_option_fields, list
        ):
            projected_questions.append(question)
            continue
        projected_options: list[Any] = []
        for option_index, raw_option in enumerate(raw_options):
            option_path = f"{question_path}.options[{option_index}]"
            option = project_mapping_fields(
                raw_option,
                allowed_fields=tuple(str(item) for item in required_option_fields),
                path=option_path,
                mutations=mutations,
            )
            if not isinstance(option, dict):
                projected_options.append(option)
                continue
            typed_value = option.get("typed_value")
            if isinstance(typed_value, Mapping):
                typed_path = f"{option_path}.typed_value"
                typed_shape = option_contract.get("typed_value_shape")
                typed_variants = option_contract.get("typed_value_variants")
                allowed_typed_fields: tuple[str, ...] | None = None
                if isinstance(typed_shape, Mapping):
                    allowed_typed_fields = tuple(str(key) for key in typed_shape)
                elif isinstance(typed_variants, Mapping):
                    variant = typed_variants.get(typed_value.get("kind"))
                    if isinstance(variant, Mapping):
                        allowed_typed_fields = tuple(str(key) for key in variant)
                if allowed_typed_fields is not None:
                    if (
                        contract.get("slot", {}).get("slot_kind")
                        == "phase_aggregation"
                        and "value_ref" in typed_value
                    ):
                        duplicate_ref = typed_value.get("value_ref")
                        option_ref = option.get("value_ref")
                        if duplicate_ref != option_ref:
                            raise LLMOutputError(
                                "single_authority_clarification_projection_conflict:"
                                f"{typed_path}.value_ref"
                            )
                    option["typed_value"] = project_mapping_fields(
                        typed_value,
                        allowed_fields=allowed_typed_fields,
                        path=typed_path,
                        mutations=mutations,
                    )
            projected_options.append(option)
        question["options"] = projected_options
        projected_questions.append(question)
    projected["questions"] = projected_questions
    return ContractProjection.create(output=projected, mutations=mutations)


def _validate_single_authority_clarification_output(
    output: Mapping[str, Any],
    *,
    slot: Mapping[str, Any],
    time_spec: Mapping[str, Any],
    required_recommended_value_ref: str,
    required_recommended_label: str,
    required_recommended_typed_value: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(output, Mapping) or set(output) != {
        "question",
        "options",
        "recommendation_reason",
        "status_message",
    }:
        raise LLMOutputError("single_authority_clarification_shape_invalid")
    for field in ("question", "recommendation_reason", "status_message"):
        if not isinstance(output[field], str) or not output[field].strip():
            raise LLMOutputError("single_authority_clarification_narrative_invalid")
    raw_options = output.get("options")
    if (
        not isinstance(raw_options, list)
        or len(raw_options) not in {2, 3}
        or any(not isinstance(option, Mapping) for option in raw_options)
    ):
        raise LLMOutputError("single_authority_clarification_options_invalid")
    expected_fields = {
        "value_ref",
        "label",
        "description",
        "recommended",
    }
    slot_kind = str(slot.get("slot_kind") or "")
    typed_comparison = slot_kind in {
        "comparison_window",
        "comparison_interpretation",
    }
    typed_decision = typed_comparison or slot_kind in {
        "month_phase_definition",
        "phase_aggregation",
    }
    if typed_decision:
        expected_fields.add("typed_value")
    allowed_refs = set(slot.get("allowed_value_refs") or ())
    normalized: list[dict[str, Any]] = []
    for option in raw_options:
        if set(option) != expected_fields:
            raise LLMOutputError("single_authority_clarification_option_shape_invalid")
        value_ref = option.get("value_ref")
        label = option.get("label")
        description = option.get("description")
        recommended = option.get("recommended")
        if (
            not isinstance(value_ref, str)
            or value_ref not in allowed_refs
            or not isinstance(label, str)
            or not label.strip()
            or not isinstance(description, str)
            or not description.strip()
            or not isinstance(recommended, bool)
        ):
            raise LLMOutputError("single_authority_clarification_option_invalid")
        normalized_option = dict(option)
        if typed_comparison:
            typed_value = option.get("typed_value")
            try:
                normalized_typed_value = validate_comparison_spec(
                    typed_value,
                    time_spec=time_spec,
                )
            except (TypeError, ValueError) as exc:
                raise LLMOutputError(
                    _clarification_typed_value_failure_code(
                        slot=slot,
                        typed_value=typed_value,
                        cause=exc,
                    )
                ) from exc
            if normalized_typed_value["kind"] not in {
                "fixed_window",
                "calendar_partition",
            }:
                raise LLMOutputError(
                    "single_authority_clarification_typed_value_invalid"
                )
            if (
                slot_kind == "comparison_window"
                and normalized_typed_value["baseline_class"] != value_ref
            ):
                raise LLMOutputError(
                    "single_authority_clarification_typed_value_invalid"
                )
            normalized_option["typed_value"] = canonical_value(normalized_typed_value)
        elif typed_decision:
            typed_value = option.get("typed_value")
            try:
                normalized_typed_value, normalized_value_ref = (
                    normalize_temporal_decision_value(
                        slot_id=str(slot["slot_id"]),
                        value=typed_value,
                        time_spec=time_spec,
                    )
                )
            except (TypeError, ValueError) as exc:
                raise LLMOutputError(
                    _clarification_typed_value_failure_code(
                        slot=slot,
                        typed_value=typed_value,
                        cause=exc,
                    )
                ) from exc
            if normalized_value_ref != value_ref:
                raise LLMOutputError(
                    "single_authority_clarification_typed_value_invalid"
                )
            normalized_option["typed_value"] = canonical_value(
                normalized_typed_value
            )
        if recommended != label.endswith("（推荐）"):
            raise LLMOutputError(
                "single_authority_clarification_recommendation_label_invalid"
            )
        normalized.append(canonical_value(normalized_option))
    if (
        len({option["value_ref"] for option in normalized}) != len(normalized)
        or sum(bool(option["recommended"]) for option in normalized) != 1
    ):
        raise LLMOutputError("single_authority_clarification_recommendation_invalid")
    if typed_decision and len(
        {
            canonical_digest(option["typed_value"])
            for option in normalized
        }
    ) != len(normalized):
        raise LLMOutputError(
            "single_authority_clarification_typed_value_duplicate"
        )
    recommended = next(option for option in normalized if option["recommended"])
    if required_recommended_value_ref and (
        recommended["value_ref"] != required_recommended_value_ref
        or (
            required_recommended_label
            and recommended["label"] != required_recommended_label
        )
    ):
        raise LLMOutputError(
            "single_authority_clarification_recommendation_contract_invalid"
        )
    if required_recommended_typed_value is not None:
        try:
            normalized_required, _ = normalize_temporal_decision_value(
                slot_id=str(slot["slot_id"]),
                value=required_recommended_typed_value,
                time_spec=time_spec,
            )
        except (TypeError, ValueError) as exc:
            raise LLMOutputError(
                "single_authority_clarification_recommendation_contract_invalid"
            ) from exc
        if canonical_value(recommended.get("typed_value")) != canonical_value(
            normalized_required
        ):
            raise LLMOutputError(
                "single_authority_clarification_recommendation_contract_invalid"
            )
    return normalized


def _clarification_typed_value_failure_code(
    *,
    slot: Mapping[str, Any],
    typed_value: Any,
    cause: BaseException,
) -> str:
    slot_id = str(slot.get("slot_id") or "unknown")
    detail = _exception_reason(cause)
    if not isinstance(typed_value, Mapping):
        detail = "typed_value_mapping_required"
    else:
        kind = typed_value.get("kind")
        if kind == "calendar_partition":
            member_definitions = typed_value.get("member_definitions")
            if not isinstance(member_definitions, (list, tuple)):
                detail = "member_definitions_list_required"
            else:
                target_members = typed_value.get("target_members")
                baseline_members = typed_value.get("baseline_members")
                if not isinstance(target_members, (list, tuple)):
                    detail = "target_members_list_required"
                elif not isinstance(baseline_members, (list, tuple)):
                    detail = "baseline_members_list_required"
                elif set(target_members).intersection(baseline_members):
                    detail = "target_baseline_members_must_be_disjoint"
        elif str(slot.get("slot_kind") or "") == "month_phase_definition":
            member_definitions = typed_value.get("member_definitions")
            if not isinstance(member_definitions, (list, tuple)):
                detail = "member_definitions_list_required"
    return (
        "single_authority_clarification_typed_value_invalid:"
        f"slot={slot_id},detail={detail}"
    )


def _clarification_public_language_issues(
    *,
    output: Mapping[str, Any],
    slot_contracts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    raw_questions = output.get("questions")
    if not isinstance(raw_questions, list):
        return []
    issues: list[dict[str, Any]] = []
    for raw_question, contract in zip(
        raw_questions,
        slot_contracts,
        strict=False,
    ):
        slot = contract.get("slot")
        if not isinstance(raw_question, Mapping) or not isinstance(slot, Mapping):
            continue
        raw_options = raw_question.get("options")
        if not isinstance(raw_options, list):
            continue
        machine_identifiers: set[str] = set()
        for value in (
            slot.get("slot_id"),
            slot.get("slot_kind"),
            *(slot.get("allowed_value_refs") or ()),
        ):
            _collect_machine_identifiers(value, machine_identifiers)
        for option in raw_options:
            if not isinstance(option, Mapping):
                continue
            _collect_machine_identifiers(
                option.get("value_ref"),
                machine_identifiers,
            )
            _collect_machine_identifiers(
                option.get("typed_value"),
                machine_identifiers,
            )
        visible_fields = {
            "question": raw_question.get("question"),
            "recommendation_reason": raw_question.get("recommendation_reason"),
            "status_message": output.get("status_message"),
            **{
                f"options[{index}].{field}": option.get(field)
                for index, option in enumerate(raw_options)
                if isinstance(option, Mapping)
                for field in ("label", "description")
            },
        }
        exposed_fields = sorted(
            field
            for field, text in visible_fields.items()
            if isinstance(text, str)
            and any(
                re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(identifier)}"
                    rf"(?![A-Za-z0-9_])",
                    text,
                    flags=re.IGNORECASE,
                )
                for identifier in machine_identifiers
            )
        )
        if exposed_fields:
            issues.append(
                {
                    "issue_code": "machine_identifier_exposed",
                    "slot_id": str(slot.get("slot_id") or ""),
                    "visible_fields": exposed_fields,
                }
            )
    return issues


def _collect_machine_identifiers(value: Any, target: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            _collect_machine_identifiers(key, target)
            _collect_machine_identifiers(nested, target)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _collect_machine_identifiers(nested, target)
        return
    if isinstance(value, str) and re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_]*",
        value,
    ):
        target.add(value)


def _persist_clarification(state: WorkflowState) -> WorkflowState:
    if state.get("intent_revision"):
        authority_store = state["request"].get("authority_store")
        if not callable(getattr(authority_store, "save_waiting_transition", None)):
            raise WorkflowFailure(
                "single_authority_store_missing", failure_type="contract"
            )
        parent_transition_id = str(state.get("durable_transition_id") or "")
        waiting_input = {
            "intent_revision_id": state["intent_revision"]["intent_revision_id"],
            "decision_ledger_position": int(state.get("decision_ledger_position") or 0),
            "decision_options_digest": canonical_digest(
                state.get("decision_options") or ()
            ),
            "clarification_digest": canonical_digest(
                state.get("clarification_outcome") or {}
            ),
            "parent_transition_id": parent_transition_id,
        }
        waiting_input_digest = canonical_digest(waiting_input)
        accepted = authority_store.load_accepted_transition(
            run_attempt_id=state["run_id"],
            node_name="persist_waiting_for_decision",
            input_digest=waiting_input_digest,
        )
        if accepted is not None:
            waiting_output = accepted.get("output_payload") or {}
            try:
                lifecycle = LifecycleState.from_dict(waiting_output["lifecycle_state"])
            except (KeyError, TypeError, ValueError) as exc:
                raise WorkflowFailure(
                    "accepted_waiting_transition_invalid",
                    failure_type="persistence",
                ) from exc
            transition = accepted["transition"]
        else:
            lifecycle = authority_store.latest_lifecycle_state(state["run_id"])
            if lifecycle is None:
                raise WorkflowFailure(
                    "clarification_lifecycle_missing",
                    failure_type="persistence",
                )
            if (
                lifecycle.execution_state != "waiting"
                or lifecycle.interaction_state != "waiting_for_user"
            ):
                lifecycle = lifecycle.transition(
                    execution_state="waiting",
                    interaction_state="waiting_for_user",
                )
            waiting_output = {
                "status": "waiting_for_clarification",
                "lifecycle_state": lifecycle.to_dict(),
            }
            transition = DurableTransition.create(
                node_name="persist_waiting_for_decision",
                parent_transition_id=parent_transition_id,
                run_attempt_id=state["run_id"],
                intent_revision_id=state["intent_revision"]["intent_revision_id"],
                decision_ledger_position=int(
                    state.get("decision_ledger_position") or 0
                ),
                input_digest=waiting_input_digest,
                output_digest=canonical_digest(waiting_output),
                execution_attempt=1,
                provider_ref="local_deterministic",
                model_ref="contract_policy",
                status="succeeded",
                acceptance_state="accepted",
                next_transition="await_user_decision",
            )
            try:
                authority_store.save_waiting_transition(
                    transition=transition,
                    lifecycle=lifecycle,
                    input_payload=waiting_input,
                    output_payload=waiting_output,
                )
            except Exception as exc:
                raise WorkflowFailure(
                    f"waiting_checkpoint_persistence_failed:{_exception_reason(exc)}",
                    failure_type="persistence",
                ) from exc
        state["durable_transition_id"] = transition.transition_id
        state["durable_checkpoint"] = transition.to_dict()
        interaction_result = {
            "schema_version": "single-authority-phase01.v1",
            "run_id": state["run_id"],
            "run_attempt_id": state["run_id"],
            "status": "waiting_for_clarification",
            "intent_revision": to_jsonable(state["intent_revision"]),
            "raw_intent_output": to_jsonable(state.get("raw_intent_output") or {}),
            "decision_ledger": {
                "position": int(state.get("decision_ledger_position") or 0),
                "records": to_jsonable(state.get("decision_ledger") or ()),
            },
            "clarification": to_jsonable(state.get("clarification_outcome") or {}),
            "raw_clarification_output": to_jsonable(
                state.get("raw_clarification_output") or {}
            ),
            "durable_checkpoint": to_jsonable(state.get("durable_checkpoint") or {}),
            "lifecycle_state": to_jsonable(
                lifecycle.to_dict() if lifecycle is not None else {}
            ),
            "authority_refs": {
                "intent_revision_id": state["intent_revision"]["intent_revision_id"],
                "decision_ledger_position": int(
                    state.get("decision_ledger_position") or 0
                ),
                "accepted_transition_id": str(state.get("durable_transition_id") or ""),
            },
            "llm_calls": to_jsonable(state.get("llm_calls") or ()),
            "checkpoint_events": to_jsonable(state.get("checkpoint_events") or ()),
        }
        state["workflow_status"] = "waiting_for_clarification"
        state["interaction_result"] = interaction_result
        return state
    raise WorkflowFailure(
        "single_authority_intent_revision_missing",
        failure_type="contract",
    )


def _compile_authoritative_plan(state: WorkflowState) -> WorkflowState:
    request = state["request"]
    authority_store = request.get("authority_store")
    required_store_methods = (
        "load_authority_context",
        "resolve_active_plan_revision",
        "load_planner_proposal",
        "load_proposal_admission",
        "save_plan_revision_transition",
        "load_decision_ledger",
        "load_accepted_transition",
        "list_dataset_snapshots",
    )
    if not all(
        callable(getattr(authority_store, method, None))
        for method in required_store_methods
    ):
        raise WorkflowFailure(
            "single_authority_plan_store_missing", failure_type="contract"
        )
    registry = request["runtime_registry"]
    if not isinstance(registry, RuntimeContractRegistry):
        raise WorkflowFailure(
            "single_authority_plan_registry_invalid", failure_type="contract"
        )
    try:
        intent_revision = IntentRevision.from_dict(state["intent_revision"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowFailure(
            "single_authority_plan_intent_invalid", failure_type="contract"
        ) from exc
    decision_ledger = authority_store.load_decision_ledger(
        intent_revision.intent_revision_id
    )
    decision_refs = tuple(
        record.decision_id for record in decision_ledger.active_records()
    )
    if decision_ledger.position != int(state.get("decision_ledger_position") or 0):
        raise WorkflowFailure(
            "single_authority_plan_decision_ledger_stale",
            failure_type="persistence",
        )

    active_plan = authority_store.resolve_active_plan_revision(
        intent_revision.run_attempt_id
    )
    if active_plan is not None:
        context = authority_store.load_authority_context(intent_revision.run_attempt_id)
        proposal = authority_store.load_planner_proposal(
            active_plan.planner_proposal_ref
        )
        admission = authority_store.load_proposal_admission(
            active_plan.proposal_admission_ref
        )
        if context is None or proposal is None or admission is None:
            raise WorkflowFailure(
                "accepted_plan_bundle_incomplete", failure_type="persistence"
            )
        transition, planner_audit, plan_patch_ref = _load_accepted_plan_transition(
            authority_store=authority_store,
            authority_context=context,
            planner_proposal=proposal,
            proposal_admission=admission,
            plan_revision=active_plan,
        )
        plan_stage_name = (
            "compile_plan_patch"
            if active_plan.supersedes_plan_revision_id is not None
            else "compile_authoritative_plan"
        )
        planner_task = (
            "single_authority_plan_patch_proposal"
            if active_plan.supersedes_plan_revision_id is not None
            else "single_authority_plan_proposal"
        )
        _validate_provider_stage_seal(
            state,
            transition=transition,
            stage_name=plan_stage_name,
        )
        if not any(
            isinstance(item, Mapping)
            and item.get("task") == planner_task
            and item.get("raw_response_content")
            == planner_audit["raw_response_content"]
            for item in state.get("llm_calls") or ()
        ):
            state.setdefault("llm_calls", []).append(planner_audit)
        return _bind_planned_result(
            state,
            authority_context=context,
            planner_proposal=proposal,
            proposal_admission=admission,
            plan_revision=active_plan,
            transition=transition,
            decision_ledger_position=decision_ledger.position,
            plan_patch_ref=plan_patch_ref,
        )

    persisted_context = authority_store.load_authority_context(
        intent_revision.run_attempt_id
    )
    if persisted_context is not None:
        raise WorkflowFailure(
            "authority_context_without_active_plan", failure_type="persistence"
        )
    release_resolver = request["release_resolver"]
    try:
        authority_context = resolve_latest_authority_context(
            run_attempt_id=intent_revision.run_attempt_id,
            actual_as_of=datetime.now(timezone.utc),
            runtime_registry=registry,
            snapshot_records=authority_store.list_dataset_snapshots(),
            release_resolver=release_resolver,
        )
    except PlanAuthorityContractError as exc:
        raise WorkflowFailure(
            f"authority_context_resolution_failed:{_exception_reason(exc)}",
            failure_type="contract",
        ) from exc

    proposal_payload = _single_authority_plan_proposal_payload(
        intent_revision=intent_revision,
        decision_ledger=decision_ledger,
        authority_context=authority_context,
        registry=registry,
    )
    prompt = build_prompt("single_authority_plan_proposal", proposal_payload)

    def validate_provider_output(candidate: Mapping[str, Any]) -> None:
        _planner_proposal_from_provider_output(
            candidate,
            intent_revision=intent_revision,
            decision_refs=decision_refs,
            authority_context=authority_context,
            prompt_version=prompt.prompt_version,
            model_version=_workflow_model_ref(state),
        )

    raw_proposal = _invoke_llm(
        state,
        "single_authority_plan_proposal",
        proposal_payload,
        output_validator=validate_provider_output,
    )
    last_audit = next(
        (
            item
            for item in reversed(state.get("llm_calls") or ())
            if isinstance(item, Mapping)
            and item.get("task") == "single_authority_plan_proposal"
        ),
        None,
    )
    planner_audit = _validated_planner_provider_audit(last_audit)
    planner_proposal = _planner_proposal_from_provider_output(
        raw_proposal,
        intent_revision=intent_revision,
        decision_refs=decision_refs,
        authority_context=authority_context,
        prompt_version=prompt.prompt_version,
        model_version=str(planner_audit["model"]),
        raw_provider_response_ref=_planner_raw_response_ref(planner_audit),
    )
    try:
        compile_result = AuthoritativePlanCompiler(runtime_registry=registry).compile(
            intent_revision=intent_revision,
            decision_ledger=decision_ledger,
            authority_context=authority_context,
            planner_proposal=planner_proposal,
        )
    except PlanAuthorityContractError as exc:
        raise WorkflowFailure(
            f"authoritative_plan_compile_failed:{_exception_reason(exc)}",
            failure_type="contract",
        ) from exc

    proposal_admission = compile_result.proposal_admission
    plan_revision = compile_result.plan_revision
    input_payload, output_payload = _plan_transition_payloads(
        authority_context=authority_context,
        planner_proposal=planner_proposal,
        proposal_admission=proposal_admission,
        plan_revision=plan_revision,
        plan_patch_ref=None,
        planner_llm_audit=planner_audit,
    )
    transition = DurableTransition.create(
        node_name="compile_authoritative_plan",
        parent_transition_id=(str(state.get("durable_transition_id") or "") or None),
        run_attempt_id=intent_revision.run_attempt_id,
        intent_revision_id=intent_revision.intent_revision_id,
        decision_ledger_position=decision_ledger.position,
        input_digest=canonical_digest(input_payload),
        output_digest=canonical_digest(output_payload),
        execution_attempt=1,
        provider_ref=str(planner_audit["provider"]),
        model_ref=str(planner_audit["model"]),
        status="succeeded",
        acceptance_state="accepted",
        next_transition="phase02_plan_bound",
    )
    try:
        authority_store.save_plan_revision_transition(
            authority_context=authority_context,
            planner_proposal=planner_proposal,
            proposal_admission=proposal_admission,
            plan_revision=plan_revision,
            transition=transition,
            input_payload=input_payload,
            output_payload=output_payload,
            accepted_attempt_refs=_provider_attempt_refs(
                state,
                stage_name="compile_authoritative_plan",
            ),
            plan_patch=None,
        )
    except Exception as exc:
        raise WorkflowFailure(
            f"plan_checkpoint_persistence_failed:{_exception_reason(exc)}",
            failure_type="persistence",
        ) from exc
    return _bind_planned_result(
        state,
        authority_context=authority_context,
        planner_proposal=planner_proposal,
        proposal_admission=proposal_admission,
        plan_revision=plan_revision,
        transition=transition,
        decision_ledger_position=decision_ledger.position,
        plan_patch_ref=None,
    )


def _single_authority_plan_proposal_payload(
    *,
    intent_revision: IntentRevision,
    decision_ledger: DecisionLedger,
    authority_context: AuthorityContext,
    registry: RuntimeContractRegistry,
) -> dict[str, Any]:
    relevant_axes = [
        axis_id
        for axis_id in registry.analysis_axis_ids
        if set(intent_revision.target_metric_refs).issubset(
            set(registry.analysis_axis(axis_id)["target_metric_refs"])
        )
    ]
    relevant_capabilities = tuple(
        dict.fromkeys(
            capability_id
            for axis_id in relevant_axes
            for capability_id in registry.analysis_axis(axis_id)["capability_refs"]
        )
    )
    return {
        "intent_revision": intent_revision.to_dict(),
        "active_decisions": [
            record.to_dict() for record in decision_ledger.active_records()
        ],
        "authority_context": authority_context.to_dict(),
        "goal_contracts": [
            {
                "goal_id": str(binding["goal_id"]),
                **registry.analysis_goal_obligation(str(binding["goal_id"])),
            }
            for binding in intent_revision.goal_bindings
        ],
        "analysis_axis_catalog": [
            {"axis_id": axis_id, **registry.analysis_axis(axis_id)}
            for axis_id in relevant_axes
        ],
        "capability_summaries": [
            _planner_capability_summary(registry, capability_id)
            for capability_id in relevant_capabilities
        ],
    }


def _planner_capability_summary(
    registry: RuntimeContractRegistry, capability_id: str
) -> dict[str, Any]:
    contract = registry.capability_inputs(capability_id)
    return {
        "capability_id": capability_id,
        "supported_claim_types": list(contract.get("supported_claim_types") or ()),
        "supported_evidence_types": list(
            contract.get("supported_evidence_types") or ()
        ),
        "allowed_datasets": list(contract.get("allowed_datasets") or ()),
        "allowed_context_datasets": list(
            contract.get("allowed_context_datasets") or ()
        ),
        "degradation_policy": canonical_value(contract.get("degradation_policy") or {}),
        "contract_ref": registry.capability_contract_ref(capability_id),
    }


def _planner_proposal_from_provider_output(
    output: Mapping[str, Any],
    *,
    intent_revision: IntentRevision,
    decision_refs: Sequence[str],
    authority_context: AuthorityContext,
    prompt_version: str,
    model_version: str,
    raw_provider_response_ref: str | None = None,
) -> PlannerProposal:
    expected = {
        "issue_tree",
        "auxiliary_axes",
        "hypotheses",
        "priority_proposals",
        "assumption_proposals",
    }
    if not isinstance(output, Mapping) or set(output) != expected:
        raise LLMOutputError("planner_proposal_output_shape_invalid")
    try:
        return PlannerProposal.create(
            run_attempt_id=intent_revision.run_attempt_id,
            intent_revision_id=intent_revision.intent_revision_id,
            decision_refs=tuple(decision_refs),
            authority_context_ref=authority_context.authority_context_ref,
            issue_tree=output["issue_tree"],
            auxiliary_axes=output["auxiliary_axes"],
            hypotheses=output["hypotheses"],
            priority_proposals=output["priority_proposals"],
            assumption_proposals=output["assumption_proposals"],
            raw_provider_response_ref=(
                raw_provider_response_ref
                or "restricted-provider-response:validation:sha256:"
                + canonical_digest(output)
            ),
            schema_version="planner-proposal.v2",
            prompt_version=prompt_version,
            model_version=model_version,
        )
    except (TypeError, ValueError) as exc:
        raise LLMOutputError(_exception_reason(exc)) from exc


def _validated_planner_provider_audit(
    audit: Mapping[str, Any] | None,
    *,
    expected_task: str = "single_authority_plan_proposal",
    failure_type: str = "llm",
) -> dict[str, Any]:
    if not isinstance(audit, Mapping):
        raise WorkflowFailure(
            "planner_provider_audit_missing", failure_type=failure_type
        )
    raw_response = audit.get("raw_response_content")
    structured_output = audit.get("structured_output")
    provider = audit.get("provider")
    model = audit.get("model")
    if (
        audit.get("task") != expected_task
        or not isinstance(raw_response, str)
        or not raw_response
        or not isinstance(structured_output, Mapping)
        or not isinstance(provider, str)
        or not provider
        or not isinstance(model, str)
        or not model
    ):
        raise WorkflowFailure(
            "planner_provider_audit_invalid", failure_type=failure_type
        )
    return canonical_value(audit)


def _planner_raw_response_ref(audit: Mapping[str, Any]) -> str:
    return planner_raw_response_ref(str(audit["raw_response_content"]))


def _plan_transition_payloads(
    *,
    authority_context: AuthorityContext,
    planner_proposal: PlannerProposal,
    proposal_admission: ProposalAdmissionRecord,
    plan_revision: PlanRevision,
    plan_patch_ref: str | None,
    planner_llm_audit: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {
            "intent_revision_id": plan_revision.intent_revision_id,
            "decision_refs": list(plan_revision.decision_refs),
            "authority_context_ref": authority_context.authority_context_ref,
            "planner_proposal_ref": planner_proposal.planner_proposal_id,
            "proposal_admission_ref": (proposal_admission.proposal_admission_id),
            "supersedes_plan_revision_id": (plan_revision.supersedes_plan_revision_id),
            "plan_patch_ref": plan_patch_ref,
        },
        {
            "authority_context": authority_context.to_dict(),
            "planner_proposal": planner_proposal.to_dict(),
            "proposal_admission_record": proposal_admission.to_dict(),
            "plan_revision": plan_revision.to_dict(),
            **(
                {"planner_llm_audit": canonical_value(planner_llm_audit)}
                if planner_llm_audit is not None
                else {}
            ),
        },
    )


def _load_accepted_plan_transition(
    *,
    authority_store: Any,
    authority_context: AuthorityContext,
    planner_proposal: PlannerProposal,
    proposal_admission: ProposalAdmissionRecord,
    plan_revision: PlanRevision,
) -> tuple[DurableTransition, dict[str, Any], str | None]:
    plan_patch_ref: str | None = None
    if plan_revision.supersedes_plan_revision_id is not None:
        loader = getattr(authority_store, "load_plan_revision_transition", None)
        if not callable(loader):
            raise WorkflowFailure(
                "accepted_plan_patch_transition_loader_missing",
                failure_type="contract",
            )
        accepted = loader(plan_revision.plan_revision_id)
        if not isinstance(accepted, Mapping):
            raise WorkflowFailure(
                "accepted_plan_transition_missing", failure_type="persistence"
            )
        accepted_input = accepted.get("input_payload")
        if not isinstance(accepted_input, Mapping):
            raise WorkflowFailure(
                "accepted_plan_transition_invalid", failure_type="persistence"
            )
        raw_plan_patch_ref = accepted_input.get("plan_patch_ref")
        if not isinstance(raw_plan_patch_ref, str) or not raw_plan_patch_ref:
            raise WorkflowFailure(
                "accepted_plan_patch_ref_missing", failure_type="persistence"
            )
        plan_patch_ref = raw_plan_patch_ref
    input_payload, expected_records = _plan_transition_payloads(
        authority_context=authority_context,
        planner_proposal=planner_proposal,
        proposal_admission=proposal_admission,
        plan_revision=plan_revision,
        plan_patch_ref=plan_patch_ref,
    )
    if plan_revision.supersedes_plan_revision_id is None:
        accepted = authority_store.load_accepted_transition(
            run_attempt_id=plan_revision.run_attempt_id,
            node_name="compile_authoritative_plan",
            input_digest=canonical_digest(input_payload),
        )
    if accepted is None:
        raise WorkflowFailure(
            "accepted_plan_transition_missing", failure_type="persistence"
        )
    transition = accepted.get("transition")
    output_payload = accepted.get("output_payload")
    planner_audit = (
        output_payload.get("planner_llm_audit")
        if isinstance(output_payload, Mapping)
        else None
    )
    if (
        not isinstance(transition, DurableTransition)
        or not isinstance(output_payload, Mapping)
        or set(output_payload) != {*expected_records, "planner_llm_audit"}
        or any(
            canonical_value(output_payload.get(key)) != canonical_value(value)
            for key, value in expected_records.items()
        )
        or transition.output_digest != canonical_digest(output_payload)
        or transition.intent_revision_id != plan_revision.intent_revision_id
        or transition.acceptance_state != "accepted"
        or transition.next_transition
        != (
            "phase03_plan_patch_bound"
            if plan_revision.supersedes_plan_revision_id is not None
            else "phase02_plan_bound"
        )
    ):
        raise WorkflowFailure(
            "accepted_plan_transition_invalid", failure_type="persistence"
        )
    try:
        validated_audit = validate_planner_provider_audit_closure(
            planner_audit=planner_audit,
            planner_proposal=planner_proposal,
            transition=transition,
        )
    except EvidenceIntegrityError as exc:
        raise WorkflowFailure(
            "accepted_plan_provider_audit_mismatch",
            failure_type="persistence",
        ) from exc
    return transition, validated_audit, plan_patch_ref


def _bind_planned_result(
    state: WorkflowState,
    *,
    authority_context: AuthorityContext,
    planner_proposal: PlannerProposal,
    proposal_admission: ProposalAdmissionRecord,
    plan_revision: PlanRevision,
    transition: DurableTransition,
    decision_ledger_position: int,
    plan_patch_ref: str | None,
) -> WorkflowState:
    if (
        plan_revision.intent_revision_id
        != state["intent_revision"]["intent_revision_id"]
        or plan_revision.decision_refs != planner_proposal.decision_refs
        or plan_revision.authority_context_ref
        != authority_context.authority_context_ref
        or plan_revision.planner_proposal_ref != planner_proposal.planner_proposal_id
        or plan_revision.proposal_admission_ref
        != proposal_admission.proposal_admission_id
    ):
        raise WorkflowFailure(
            "planned_result_authority_mismatch", failure_type="persistence"
        )
    authority_refs = {
        "intent_revision_id": plan_revision.intent_revision_id,
        "authority_context_ref": authority_context.authority_context_ref,
        "planner_proposal_id": planner_proposal.planner_proposal_id,
        "proposal_admission_id": proposal_admission.proposal_admission_id,
        "plan_revision_id": plan_revision.plan_revision_id,
        "accepted_transition_id": transition.transition_id,
    }
    state["authority_context"] = authority_context.to_dict()
    state["planner_proposal"] = planner_proposal.to_dict()
    state["proposal_admission_record"] = proposal_admission.to_dict()
    state["plan_revision"] = plan_revision.to_dict()
    state["durable_transition_id"] = transition.transition_id
    state["durable_checkpoint"] = transition.to_dict()
    state["workflow_status"] = (
        "planned"
        if state.get("request", {}).get("stop_after_phase") == "phase02"
        else "plan_bound"
    )
    state["plan_result"] = {
        "schema_version": AUTHORITATIVE_PLAN_RESULT_SCHEMA_VERSION,
        "run_id": state["run_id"],
        "run_attempt_id": plan_revision.run_attempt_id,
        "status": "planned",
        "plan_patch_ref": plan_patch_ref,
        "intent_revision_id": plan_revision.intent_revision_id,
        "decision_ledger_position": decision_ledger_position,
        "decision_refs": list(plan_revision.decision_refs),
        "authority_context": authority_context.to_dict(),
        "planner_proposal": planner_proposal.to_dict(),
        "proposal_admission_record": proposal_admission.to_dict(),
        "plan_revision": plan_revision.to_dict(),
        "durable_checkpoint": transition.to_dict(),
        "authority_refs": authority_refs,
        "llm_calls": to_jsonable(state.get("llm_calls") or ()),
        "checkpoint_events": to_jsonable(state.get("checkpoint_events") or ()),
    }
    return state


def _route_after_authoritative_plan(state: WorkflowState) -> str:
    stop_after_phase = state.get("request", {}).get("stop_after_phase")
    if stop_after_phase in {None, "phase03", "phase04", "phase05"}:
        return "execute"
    if stop_after_phase == "phase02":
        return "stop"
    raise WorkflowFailure("stop_after_phase_invalid", failure_type="contract")


def _execute_capability_dag(state: WorkflowState) -> WorkflowState:
    request = state["request"]
    authority_store = request.get("authority_store")
    required_store_methods = (
        "resolve_active_plan_revision",
        "load_authority_context",
        "load_decision_ledger",
        "load_capability_outcome",
        "accept_capability_outcome",
        "load_execution_snapshot",
        "accept_execution_settlement",
        "load_accepted_transition",
    )
    if not all(
        callable(getattr(authority_store, method, None))
        for method in required_store_methods
    ):
        raise WorkflowFailure(
            "single_authority_execution_store_missing",
            failure_type="contract",
        )
    try:
        plan_revision = PlanRevision.from_dict(state["plan_revision"])
        intent_revision = IntentRevision.from_dict(state["intent_revision"])
        authority_context = AuthorityContext.from_dict(state["authority_context"])
        planner_proposal = PlannerProposal.from_dict(state["planner_proposal"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowFailure(
            "single_authority_execution_bundle_invalid",
            failure_type="contract",
        ) from exc

    active_plan = authority_store.resolve_active_plan_revision(
        plan_revision.run_attempt_id
    )
    persisted_context = authority_store.load_authority_context(
        plan_revision.run_attempt_id
    )
    decision_ledger = authority_store.load_decision_ledger(
        plan_revision.intent_revision_id
    )
    active_decision_refs = tuple(
        record.decision_id for record in decision_ledger.active_records()
    )
    if (
        active_plan != plan_revision
        or persisted_context != authority_context
        or intent_revision.intent_revision_id != plan_revision.intent_revision_id
        or intent_revision.run_attempt_id != plan_revision.run_attempt_id
        or active_decision_refs != plan_revision.decision_refs
        or decision_ledger.position != int(state.get("decision_ledger_position") or 0)
    ):
        raise WorkflowFailure(
            "single_authority_execution_head_mismatch",
            failure_type="persistence",
        )

    registry = request["runtime_registry"]
    if not isinstance(registry, RuntimeContractRegistry):
        raise WorkflowFailure(
            "single_authority_execution_registry_invalid",
            failure_type="contract",
        )
    budget_policy = registry.exploration_budget_policy
    if plan_revision.budget_policy_ref != budget_policy.budget_policy_ref:
        raise WorkflowFailure(
            "capability_execution_budget_policy_mismatch",
            failure_type="contract",
        )
    if not plan_revision.capability_tasks:
        raise WorkflowFailure(
            "capability_execution_plan_empty", failure_type="contract"
        )
    try:
        query_bundle = compile_query_bundle(
            plan_revision=plan_revision,
            planner_proposal=planner_proposal,
            runtime_registry=registry,
        )
    except QueryIRContractError as exc:
        raise WorkflowFailure(
            f"query_bundle_compile_failed:{_exception_reason(exc)}",
            failure_type="contract",
        ) from exc
    save_query_projection = getattr(
        authority_store, "save_query_bundle_projection", None
    )
    if callable(save_query_projection):
        try:
            save_query_projection(
                run_attempt_id=plan_revision.run_attempt_id,
                projection=query_bundle.customer_projection(),
            )
        except Exception as exc:
            raise WorkflowFailure(
                f"query_bundle_projection_persistence_failed:{_exception_reason(exc)}",
                failure_type="persistence",
            ) from exc
    hard_budget_limit = budget_policy.effective_hard_budget_limit(plan_revision)
    max_workers = min(4, len(plan_revision.capability_tasks))
    parent_transition_id = str(state.get("durable_transition_id") or "")
    transition_input = capability_execution_transition_input(
        plan_revision,
        hard_budget_limit=hard_budget_limit,
    )
    accepted = authority_store.load_accepted_transition(
        run_attempt_id=plan_revision.run_attempt_id,
        node_name="execute_capability_dag",
        input_digest=canonical_digest(transition_input),
    )
    if accepted is None:
        analysis_runtime = request.get("analysis_runtime")
        if analysis_runtime is None:
            raise WorkflowFailure(
                "authoritative_task_input_runtime_missing",
                failure_type="contract",
            )
        attempt_journal = getattr(authority_store, "attempt_journal", None)
        adapter_registry = builtin_capability_adapter_registry()
        adapter_registry.validate_plan(plan_revision)
        from bi_agent.runtime.authoritative_task_inputs import (
            materialize_authoritative_task_inputs,
        )

        runtime_inputs = materialize_authoritative_task_inputs(
            plan_revision=plan_revision,
            intent_revision=intent_revision,
            decision_ledger=decision_ledger,
            authority_context=authority_context,
            analysis_runtime=analysis_runtime,
            attempt_journal=attempt_journal,
            query_bundle=query_bundle,
        )
        if state.get("checkpoint_events"):
            _current_event(state)["capability_substages"] = [
                dict(item) for item in runtime_inputs.performance_observations
            ]
        adapter = adapter_registry.bind(
            plan_revision,
            runtime_inputs,
        )
        snapshot = execute_capability_plan(
            plan_revision,
            adapter=adapter,
            store=authority_store,
            settlement_authority=runtime_inputs.settlement_authority,
            attempt_journal=attempt_journal,
            upstream_accepted_attempt_refs=(runtime_inputs.accepted_query_attempt_refs),
            budget_policy=budget_policy,
            max_workers=max_workers,
            parent_transition_id=parent_transition_id,
            decision_ledger_position=decision_ledger.position,
        )
        accepted = authority_store.load_accepted_transition(
            run_attempt_id=plan_revision.run_attempt_id,
            node_name="execute_capability_dag",
            input_digest=canonical_digest(transition_input),
        )
    else:
        snapshot = authority_store.load_execution_snapshot(
            plan_revision.plan_revision_id
        )
        if snapshot is None:
            raise WorkflowFailure(
                "capability_execution_snapshot_missing",
                failure_type="persistence",
            )
    if not isinstance(accepted, Mapping):
        raise WorkflowFailure(
            "capability_execution_transition_missing",
            failure_type="persistence",
        )
    transition = accepted.get("transition")
    transition_output = accepted.get("output_payload")
    try:
        persisted_snapshot = ExecutionSnapshot.from_dict(
            transition_output["execution_snapshot"]
        )
        stop_record = ExplorationStopRecord.from_dict(
            transition_output["exploration_stop_record"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowFailure(
            "capability_execution_transition_output_invalid",
            failure_type="persistence",
        ) from exc
    if (
        not isinstance(transition, DurableTransition)
        or set(transition_output)
        != {
            "execution_snapshot",
            "exploration_stop_record",
        }
        or transition.parent_transition_id != parent_transition_id
        or canonical_value(accepted.get("input_payload") or {})
        != canonical_value(transition_input)
        or persisted_snapshot != snapshot
    ):
        raise WorkflowFailure(
            "capability_execution_transition_invalid",
            failure_type="persistence",
        )

    bundles = []
    for task in plan_revision.capability_tasks:
        bundle = authority_store.load_capability_outcome(
            plan_revision.plan_revision_id,
            task.task_id,
        )
        if bundle is not None:
            bundles.append(bundle)
    execution_result = AuthoritativeExecutionResult.from_records(
        plan_revision=plan_revision,
        execution_snapshot=snapshot,
        exploration_stop_record=stop_record,
        capability_outcome_bundles=tuple(bundles),
        durable_transition=transition,
    )
    settled_query_bundle = settle_query_bundle(
        query_bundle,
        execution_result.capability_outcome_bundles,
    )
    if callable(save_query_projection):
        try:
            save_query_projection(
                run_attempt_id=plan_revision.run_attempt_id,
                projection=settled_query_bundle.customer_projection(),
            )
        except Exception as exc:
            raise WorkflowFailure(
                f"query_bundle_projection_persistence_failed:{_exception_reason(exc)}",
                failure_type="persistence",
            ) from exc
    try:
        coverage_plan = compile_factor_coverage_plan(
            plan_revision=plan_revision,
            authority_context=authority_context,
            runtime_registry=registry,
        )
        coverage_result = settle_factor_coverage(
            plan=coverage_plan,
            execution_result=execution_result,
        )
        investigation_branches = build_investigation_branches(
            plan=coverage_plan,
            authority_context=authority_context,
        )
        investigation_synthesis = synthesize_factor_coverage(
            plan=coverage_plan,
            coverage_result=coverage_result,
        )
    except (TypeError, ValueError) as exc:
        raise WorkflowFailure(
            "factor_coverage_settlement_invalid",
            failure_type="contract",
        ) from exc
    state["execution_snapshot"] = snapshot.to_dict()
    state["exploration_stop_record"] = stop_record.to_dict()
    state["durable_transition_id"] = transition.transition_id
    state["durable_checkpoint"] = transition.to_dict()
    state["execution_result"] = execution_result.to_dict()
    state["authoritative_execution_result"] = execution_result
    state["query_bundle"] = settled_query_bundle.to_dict()
    state["factor_coverage_plan"] = coverage_plan.to_dict()
    state["factor_coverage_result"] = coverage_result.to_dict()
    state["investigation_branches"] = [
        branch.to_dict() for branch in investigation_branches
    ]
    state["investigation_synthesis"] = investigation_synthesis.to_dict()
    state["workflow_status"] = "evidence_ready"
    return state


def _route_after_capability_execution(state: WorkflowState) -> str:
    stop_after_phase = state.get("request", {}).get("stop_after_phase")
    if stop_after_phase in {None, "phase03", "phase04", "phase05"}:
        return "evaluate"
    raise WorkflowFailure("stop_after_phase_invalid", failure_type="contract")


def _claim_coverage_transition_input(
    evaluation: Any,
) -> dict[str, Any]:
    return {
        "source_plan_revision_id": evaluation.source_plan_revision_id,
        "source_plan_digest": evaluation.source_plan_digest,
        "source_execution_result_ref": evaluation.source_execution_result_ref,
        "source_execution_result_digest": (evaluation.source_execution_result_digest),
        "claim_coverage_evaluation_ref": evaluation.evaluation_ref,
        "claim_coverage_evaluation_digest": evaluation.content_digest,
    }


def _evaluate_claim_coverage(state: WorkflowState) -> WorkflowState:
    request = state["request"]
    authority_store = request.get("authority_store")
    if not callable(getattr(authority_store, "save_claim_coverage_transition", None)):
        raise WorkflowFailure("claim_coverage_store_missing", failure_type="contract")
    registry = request["runtime_registry"]
    if not isinstance(registry, RuntimeContractRegistry):
        raise WorkflowFailure(
            "claim_coverage_registry_invalid", failure_type="contract"
        )
    try:
        authority_context = AuthorityContext.from_dict(state["authority_context"])
        plan_revision = PlanRevision.from_dict(state["plan_revision"])
        execution_result = validate_typed_authoritative_execution_result(
            state["authoritative_execution_result"]
        )
        evaluation = evaluate_claim_coverage(
            authority_context=authority_context,
            plan_revision=plan_revision,
            execution_result=execution_result,
            route_catalog=registry,
        )
    except (KeyError, TypeError, ValueError, ClaimCoverageContractError) as exc:
        raise WorkflowFailure(
            f"claim_coverage_evaluation_failed:{_exception_reason(exc)}",
            failure_type="contract",
        ) from exc

    transition_input = _claim_coverage_transition_input(evaluation)
    accepted = authority_store.load_accepted_transition(
        run_attempt_id=plan_revision.run_attempt_id,
        node_name="evaluate_claim_coverage",
        input_digest=canonical_digest(transition_input),
    )
    if accepted is not None:
        try:
            output = accepted["output_payload"]
            transition = accepted["transition"]
            if (
                not isinstance(output, Mapping)
                or set(output)
                != {
                    "claim_coverage_evaluation",
                    "plan_expansion_decision",
                    "plan_patch",
                }
                or not isinstance(transition, DurableTransition)
            ):
                raise ClaimCoverageContractError(
                    "claim_coverage_checkpoint_shape_invalid"
                )
            replayed_evaluation = type(evaluation).from_dict(
                output["claim_coverage_evaluation"],
                authority_context=authority_context,
                plan_revision=plan_revision,
                execution_result=execution_result,
                route_catalog=registry,
            )
            decision = PlanExpansionDecision.from_dict(
                output["plan_expansion_decision"],
                evaluation=replayed_evaluation,
            )
            raw_patch = output["plan_patch"]
            plan_patch = (
                None
                if raw_patch is None
                else PlanPatch.from_dict(
                    raw_patch,
                    plan_revision=plan_revision,
                    execution_result=execution_result,
                    evaluation=replayed_evaluation,
                    decision=decision,
                )
            )
            checkpoint = ClaimCoverageCheckpoint.create(
                plan_revision=plan_revision,
                execution_result=execution_result,
                evaluation=replayed_evaluation,
                decision=decision,
                plan_patch=plan_patch,
                transition=transition,
            )
        except (KeyError, TypeError, ValueError, ClaimCoverageContractError) as exc:
            raise WorkflowFailure(
                "accepted_claim_coverage_checkpoint_invalid",
                failure_type="persistence",
            ) from exc
        if canonical_value(accepted.get("input_payload") or {}) != canonical_value(
            transition_input
        ):
            raise WorkflowFailure(
                "accepted_claim_coverage_checkpoint_invalid",
                failure_type="persistence",
            )
        expected_attempt_count = 1 if decision.decision_authority == "provider" else 0
        _validate_provider_stage_cardinality(
            state,
            transition=transition,
            stage_name="evaluate_claim_coverage",
            expected_count=expected_attempt_count,
        )
        return _bind_claim_coverage_checkpoint(state, checkpoint)

    if evaluation.admissible_routes:
        decision_payload = {
            "claim_coverage_evaluation": evaluation.to_dict(),
            "admissible_routes": [
                route.to_dict() for route in evaluation.admissible_routes
            ],
            "decision_contract": {
                "decisions": ["seal", "patch"],
                "selected_axis_ids": [
                    route.axis_id for route in evaluation.admissible_routes
                ],
                "patch_requires_non_empty_selection": True,
                "seal_requires_empty_selection": True,
            },
        }

        def validate_expansion_output(candidate: Mapping[str, Any]) -> None:
            if not isinstance(candidate, Mapping) or set(candidate) != {
                "decision",
                "selected_axis_ids",
            }:
                raise LLMOutputError("plan_expansion_provider_output_invalid")
            raw_axes = candidate.get("selected_axis_ids")
            if (
                candidate.get("decision") not in {"seal", "patch"}
                or isinstance(raw_axes, (str, bytes))
                or not isinstance(raw_axes, Sequence)
                or any(not isinstance(item, str) or not item for item in raw_axes)
                or len(raw_axes) != len(set(raw_axes))
                or not set(raw_axes).issubset(
                    {route.axis_id for route in evaluation.admissible_routes}
                )
                or (candidate.get("decision") == "patch" and not raw_axes)
                or (candidate.get("decision") == "seal" and bool(raw_axes))
            ):
                raise LLMOutputError("plan_expansion_provider_output_invalid")

        _invoke_llm(
            state,
            PLAN_EXPANSION_PROVIDER_TASK,
            decision_payload,
            output_validator=validate_expansion_output,
        )
        last_audit = next(
            (
                item
                for item in reversed(state.get("llm_calls") or ())
                if isinstance(item, Mapping)
                and item.get("task") == PLAN_EXPANSION_PROVIDER_TASK
            ),
            None,
        )
        if not isinstance(last_audit, Mapping):
            raise WorkflowFailure(
                "plan_expansion_provider_audit_missing", failure_type="llm"
            )
        audit_projection = {
            key: last_audit.get(key)
            for key in (
                "task",
                "provider",
                "model",
                "prompt_version",
                "raw_response_content",
                "structured_output",
            )
        }
        try:
            decision = PlanExpansionDecision.from_provider_audit(
                evaluation=evaluation,
                provider_audit=audit_projection,
            )
        except ClaimCoverageContractError as exc:
            raise WorkflowFailure(
                f"plan_expansion_decision_invalid:{_exception_reason(exc)}",
                failure_type="llm",
            ) from exc
    else:
        decision = PlanExpansionDecision.deterministic_seal(evaluation)

    plan_patch = (
        PlanPatch.create(
            plan_revision=plan_revision,
            execution_result=execution_result,
            evaluation=evaluation,
            decision=decision,
        )
        if decision.decision == "patch"
        else None
    )
    transition_input, transition_output = claim_coverage_transition_payloads(
        evaluation=evaluation,
        decision=decision,
        plan_patch=plan_patch,
    )
    transition = DurableTransition.create(
        node_name="evaluate_claim_coverage",
        parent_transition_id=execution_result.transition_id,
        run_attempt_id=plan_revision.run_attempt_id,
        intent_revision_id=plan_revision.intent_revision_id,
        decision_ledger_position=(
            execution_result.durable_transition.decision_ledger_position
        ),
        input_digest=canonical_digest(transition_input),
        output_digest=canonical_digest(transition_output),
        execution_attempt=1,
        provider_ref=(
            str(decision.provider_ref)
            if decision.decision_authority == "provider"
            else "local_deterministic"
        ),
        model_ref=(
            str(decision.model_ref)
            if decision.decision_authority == "provider"
            else "claim-coverage-contract.v1"
        ),
        status="succeeded",
        acceptance_state="accepted",
        next_transition=(
            "compile_plan_patch"
            if decision.decision == "patch"
            else "seal_authority_bundle"
        ),
    )
    checkpoint = ClaimCoverageCheckpoint.create(
        plan_revision=plan_revision,
        execution_result=execution_result,
        evaluation=evaluation,
        decision=decision,
        plan_patch=plan_patch,
        transition=transition,
    )
    accepted_attempt_refs = (
        _provider_attempt_refs(state, stage_name="evaluate_claim_coverage")
        if decision.decision_authority == "provider"
        else ()
    )
    try:
        authority_store.save_claim_coverage_transition(
            plan_revision=plan_revision,
            execution_result=execution_result,
            checkpoint=checkpoint,
            input_payload=transition_input,
            output_payload=transition_output,
            accepted_attempt_refs=accepted_attempt_refs,
        )
    except Exception as exc:
        raise WorkflowFailure(
            f"claim_coverage_checkpoint_persistence_failed:{_exception_reason(exc)}",
            failure_type="persistence",
        ) from exc
    return _bind_claim_coverage_checkpoint(state, checkpoint)


def _bind_claim_coverage_checkpoint(
    state: WorkflowState,
    checkpoint: ClaimCoverageCheckpoint,
) -> WorkflowState:
    state["claim_coverage_evaluation"] = checkpoint.evaluation.to_dict()
    state["plan_expansion_decision"] = checkpoint.decision.to_dict()
    state["plan_patch"] = (
        None if checkpoint.plan_patch is None else checkpoint.plan_patch.to_dict()
    )
    state["claim_coverage_checkpoint"] = checkpoint
    state["durable_transition_id"] = checkpoint.transition.transition_id
    state["durable_checkpoint"] = checkpoint.transition.to_dict()
    state["workflow_status"] = (
        "plan_patch_required"
        if checkpoint.decision.decision == "patch"
        else "evidence_ready"
    )
    return state


def _route_after_claim_coverage(state: WorkflowState) -> str:
    decision = state.get("plan_expansion_decision") or {}
    if decision.get("decision") == "patch":
        return "patch"
    if decision.get("decision") != "seal":
        raise WorkflowFailure("claim_coverage_route_invalid", failure_type="contract")
    stop_after_phase = state.get("request", {}).get("stop_after_phase")
    if stop_after_phase == "phase03":
        return "stop"
    if stop_after_phase in {None, "phase04", "phase05"}:
        return "settle"
    raise WorkflowFailure("stop_after_phase_invalid", failure_type="contract")


def _compile_plan_patch(state: WorkflowState) -> WorkflowState:
    request = state["request"]
    authority_store = request.get("authority_store")
    registry = request["runtime_registry"]
    if not isinstance(registry, RuntimeContractRegistry):
        raise WorkflowFailure(
            "single_authority_plan_registry_invalid", failure_type="contract"
        )
    try:
        source_plan = PlanRevision.from_dict(state["plan_revision"])
        intent_revision = IntentRevision.from_dict(state["intent_revision"])
        authority_context = AuthorityContext.from_dict(state["authority_context"])
        validate_typed_authoritative_execution_result(
            state["authoritative_execution_result"]
        )
        checkpoint = state["claim_coverage_checkpoint"]
        if type(checkpoint) is not ClaimCoverageCheckpoint:
            raise ClaimCoverageContractError("claim_coverage_checkpoint_invalid")
        plan_patch = checkpoint.plan_patch
        if plan_patch is None:
            raise ClaimCoverageContractError("plan_patch_missing")
    except (KeyError, TypeError, ValueError, ClaimCoverageContractError) as exc:
        raise WorkflowFailure(
            "plan_patch_source_bundle_invalid", failure_type="contract"
        ) from exc
    if (
        authority_store.resolve_active_plan_revision(source_plan.run_attempt_id)
        != source_plan
    ):
        raise WorkflowFailure(
            "plan_patch_source_plan_not_active", failure_type="persistence"
        )
    decision_ledger = authority_store.load_decision_ledger(
        intent_revision.intent_revision_id
    )
    decision_refs = tuple(
        record.decision_id for record in decision_ledger.active_records()
    )
    if decision_refs != source_plan.decision_refs:
        raise WorkflowFailure(
            "plan_patch_decision_ledger_stale", failure_type="persistence"
        )
    source_proposal = authority_store.load_planner_proposal(
        source_plan.planner_proposal_ref
    )
    if source_proposal is None:
        raise WorkflowFailure(
            "plan_patch_source_proposal_missing", failure_type="persistence"
        )
    base_payload = _single_authority_plan_proposal_payload(
        intent_revision=intent_revision,
        decision_ledger=decision_ledger,
        authority_context=authority_context,
        registry=registry,
    )
    selected_axes = set(plan_patch.selected_axis_ids)
    proposal_payload = {
        **base_payload,
        "source_plan_revision": source_plan.to_dict(),
        "source_planner_proposal": source_proposal.to_dict(),
        "claim_coverage_evaluation": checkpoint.evaluation.to_dict(),
        "plan_patch": plan_patch.to_dict(),
        "selected_axis_catalog": [
            {"axis_id": axis_id, **registry.analysis_axis(axis_id)}
            for axis_id in plan_patch.selected_axis_ids
        ],
    }
    prompt = build_prompt("single_authority_plan_patch_proposal", proposal_payload)

    def compile_candidate(
        candidate: Mapping[str, Any], *, model_version: str
    ) -> tuple[PlannerProposal, Any]:
        proposal = _planner_proposal_from_provider_output(
            candidate,
            intent_revision=intent_revision,
            decision_refs=decision_refs,
            authority_context=authority_context,
            prompt_version=prompt.prompt_version,
            model_version=model_version,
        )
        result = AuthoritativePlanCompiler(runtime_registry=registry).compile(
            intent_revision=intent_revision,
            decision_ledger=decision_ledger,
            authority_context=authority_context,
            planner_proposal=proposal,
            supersedes_plan_revision=source_plan,
        )
        _validate_plan_patch_successor(
            source_plan=source_plan,
            successor_plan=result.plan_revision,
            plan_patch=plan_patch,
        )
        return proposal, result

    def validate_patch_output(candidate: Mapping[str, Any]) -> None:
        try:
            proposal, _ = compile_candidate(
                candidate, model_version=_workflow_model_ref(state)
            )
        except (LLMOutputError, PlanAuthorityContractError, WorkflowFailure) as exc:
            raise LLMOutputError(_exception_reason(exc)) from exc
        proposed_axes = {str(item["axis_id"]) for item in proposal.auxiliary_axes}
        if not selected_axes.issubset(proposed_axes):
            raise LLMOutputError("plan_patch_selected_axes_missing")

    raw_proposal = _invoke_llm(
        state,
        "single_authority_plan_patch_proposal",
        proposal_payload,
        output_validator=validate_patch_output,
    )
    last_audit = next(
        (
            item
            for item in reversed(state.get("llm_calls") or ())
            if isinstance(item, Mapping)
            and item.get("task") == "single_authority_plan_patch_proposal"
        ),
        None,
    )
    planner_audit = _validated_planner_provider_audit(
        last_audit,
        expected_task="single_authority_plan_patch_proposal",
    )
    planner_proposal = _planner_proposal_from_provider_output(
        raw_proposal,
        intent_revision=intent_revision,
        decision_refs=decision_refs,
        authority_context=authority_context,
        prompt_version=prompt.prompt_version,
        model_version=str(planner_audit["model"]),
        raw_provider_response_ref=_planner_raw_response_ref(planner_audit),
    )
    try:
        compile_result = AuthoritativePlanCompiler(runtime_registry=registry).compile(
            intent_revision=intent_revision,
            decision_ledger=decision_ledger,
            authority_context=authority_context,
            planner_proposal=planner_proposal,
            supersedes_plan_revision=source_plan,
        )
        _validate_plan_patch_successor(
            source_plan=source_plan,
            successor_plan=compile_result.plan_revision,
            plan_patch=plan_patch,
        )
    except (PlanAuthorityContractError, WorkflowFailure) as exc:
        raise WorkflowFailure(
            f"plan_patch_compile_failed:{_exception_reason(exc)}",
            failure_type="contract",
        ) from exc
    successor_plan = compile_result.plan_revision
    proposal_admission = compile_result.proposal_admission
    transition_input, transition_output = _plan_transition_payloads(
        authority_context=authority_context,
        planner_proposal=planner_proposal,
        proposal_admission=proposal_admission,
        plan_revision=successor_plan,
        plan_patch_ref=plan_patch.plan_patch_ref,
        planner_llm_audit=planner_audit,
    )
    transition = DurableTransition.create(
        node_name="compile_plan_patch",
        parent_transition_id=checkpoint.transition.transition_id,
        run_attempt_id=successor_plan.run_attempt_id,
        intent_revision_id=successor_plan.intent_revision_id,
        decision_ledger_position=decision_ledger.position,
        input_digest=canonical_digest(transition_input),
        output_digest=canonical_digest(transition_output),
        execution_attempt=1,
        provider_ref=str(planner_audit["provider"]),
        model_ref=str(planner_audit["model"]),
        status="succeeded",
        acceptance_state="accepted",
        next_transition="phase03_plan_patch_bound",
    )
    try:
        authority_store.save_plan_revision_transition(
            authority_context=authority_context,
            planner_proposal=planner_proposal,
            proposal_admission=proposal_admission,
            plan_revision=successor_plan,
            transition=transition,
            input_payload=transition_input,
            output_payload=transition_output,
            accepted_attempt_refs=_provider_attempt_refs(
                state, stage_name="compile_plan_patch"
            ),
            plan_patch=plan_patch,
        )
    except Exception as exc:
        raise WorkflowFailure(
            f"plan_patch_checkpoint_persistence_failed:{_exception_reason(exc)}",
            failure_type="persistence",
        ) from exc
    return _bind_planned_result(
        state,
        authority_context=authority_context,
        planner_proposal=planner_proposal,
        proposal_admission=proposal_admission,
        plan_revision=successor_plan,
        transition=transition,
        decision_ledger_position=decision_ledger.position,
        plan_patch_ref=plan_patch.plan_patch_ref,
    )


def _validate_plan_patch_successor(
    *,
    source_plan: PlanRevision,
    successor_plan: PlanRevision,
    plan_patch: PlanPatch,
) -> None:
    source_axes = {item.axis_id for item in source_plan.analysis_axes}
    successor_axes = {item.axis_id for item in successor_plan.analysis_axes}
    source_task_keys = {item.task_key for item in source_plan.capability_tasks}
    successor_task_keys = {item.task_key for item in successor_plan.capability_tasks}
    source_obligations = {item.obligation_id for item in source_plan.claim_obligations}
    successor_obligations = {
        item.obligation_id for item in successor_plan.claim_obligations
    }
    selected_axes = set(plan_patch.selected_axis_ids)
    if (
        plan_patch.source_plan_revision_id != source_plan.plan_revision_id
        or successor_plan.supersedes_plan_revision_id != source_plan.plan_revision_id
        or successor_plan.run_attempt_id != source_plan.run_attempt_id
        or successor_plan.intent_revision_id != source_plan.intent_revision_id
        or successor_plan.decision_refs != source_plan.decision_refs
        or successor_plan.authority_context_ref != source_plan.authority_context_ref
        or successor_plan.resolved_window_refs != source_plan.resolved_window_refs
        or successor_plan.budget_policy_ref != source_plan.budget_policy_ref
        or canonical_value(successor_plan.contract_versions)
        != canonical_value(source_plan.contract_versions)
        or not source_axes.issubset(successor_axes)
        or successor_axes - source_axes != selected_axes
        or not source_task_keys.issubset(successor_task_keys)
        or not source_obligations.issubset(successor_obligations)
        or not set(plan_patch.selected_obligation_ids).issubset(successor_obligations)
    ):
        raise WorkflowFailure(
            "plan_patch_successor_closure_invalid", failure_type="contract"
        )


def _settle_claim_authority(state: WorkflowState) -> WorkflowState:
    return _run_post_execution_stage(state, stop_after="phase04")


def _compose_claim_aware_narrative(state: WorkflowState) -> WorkflowState:
    return _run_post_execution_stage(state, stop_after="phase05")


def _deliver_publication(state: WorkflowState) -> WorkflowState:
    return _run_post_execution_stage(state, stop_after=None)


def _run_post_execution_stage(
    state: WorkflowState,
    *,
    stop_after: str | None,
) -> WorkflowState:
    request = state["request"]
    prior_result = state.get("post_execution_result")
    try:
        typed_execution = state.get("authoritative_execution_result")
        if typed_execution is None and prior_result is not None:
            typed_execution = prior_result.semantic_authority_result.authority_bundle_inputs.execution_result
        if typed_execution is None:
            execution_result = AuthoritativeExecutionResult.from_dict(
                state["execution_result"]
            )
        else:
            execution_result = validate_typed_authoritative_execution_result(
                typed_execution
            )
            serialized_execution = state["execution_result"]
            if (
                not isinstance(serialized_execution, Mapping)
                or serialized_execution.get("authoritative_execution_result_ref")
                != execution_result.authoritative_execution_result_ref
                or serialized_execution.get("content_digest")
                != execution_result.content_digest
                or serialized_execution.get("run_attempt_id")
                != execution_result.run_attempt_id
                or serialized_execution.get("intent_revision_id")
                != execution_result.intent_revision_id
            ):
                raise ValueError("typed_execution_result_closure_invalid")
        intent_revision = IntentRevision.from_dict(state["intent_revision"])
        claim_coverage_checkpoint = state["claim_coverage_checkpoint"]
        if type(claim_coverage_checkpoint) is not ClaimCoverageCheckpoint:
            raise ValueError("claim_coverage_checkpoint_invalid")
        factor_coverage_plan = FactorCoveragePlan.from_dict(
            state["factor_coverage_plan"]
        )
        factor_coverage_result = FactorCoverageResult.from_dict(
            state["factor_coverage_result"],
            plan=factor_coverage_plan,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowFailure(
            "post_execution_authority_input_invalid",
            failure_type="contract",
        ) from exc
    required_values = {
        "owner_ref": request.get("owner_ref"),
        "thread_ref": request.get("thread_id"),
        "authority_connection": request.get("authority_connection"),
        "locale": request.get("locale"),
        "destination_ref": request.get("destination_ref"),
        "publication_channel": request.get("publication_channel"),
    }
    if any(value is None or value == "" for value in required_values.values()):
        raise WorkflowFailure(
            "post_execution_runtime_binding_missing",
            failure_type="contract",
        )
    runtime_registry = request.get("runtime_registry")
    if type(
        runtime_registry
    ) is not RuntimeContractRegistry or not runtime_registry.source_is_current(
        CANONICAL_RUNTIME_BINDINGS_PATH
    ):
        raise WorkflowFailure(
            "post_execution_runtime_registry_invalid",
            failure_type="contract",
        )
    visibility_policy = PublicationFieldVisibilityPolicy.fixed(
        policy_id="waje-customer-publication",
        revision=1,
        restricted_output_policy_ref=(runtime_registry.restricted_output_policy_ref),
        restricted_output_policy_version=(
            runtime_registry.restricted_output_policy_version
        ),
        restricted_output_fields=runtime_registry.restricted_output_fields,
    )
    result = run_post_execution_workflow(
        execution_result,
        claim_coverage_checkpoint=claim_coverage_checkpoint,
        intent_revision=intent_revision,
        owner_ref=str(required_values["owner_ref"]),
        thread_ref=str(required_values["thread_ref"]),
        authority_store=request.get("authority_store"),
        connection=required_values["authority_connection"],
        llm_client=state.get("llm_client"),
        visibility_policy=visibility_policy,
        sensitive_output_inspector=(
            FixedSensitiveOutputInspector.from_visibility_policy(visibility_policy)
        ),
        locale=str(required_values["locale"]),
        destination_ref=str(required_values["destination_ref"]),
        channel=str(required_values["publication_channel"]),
        transport=request.get("delivery_transport"),
        customer_term_labels={
            metric_id: runtime_registry.metric_business_labels(metric_id)[0]
            for metric_id in runtime_registry.metric_ids
        },
        stop_after=stop_after,
        prior_result=prior_result,
        factor_coverage_plan=factor_coverage_plan,
        factor_coverage_result=factor_coverage_result,
        controlled_investigation_enabled=bool(
            request.get("controlled_investigation_enabled", False)
        ),
    )
    if type(result) is not PostExecutionWorkflowResult:
        raise WorkflowFailure(
            "post_execution_result_invalid",
            failure_type="contract",
        )
    state["post_execution_result"] = result
    state["investigation_synthesis"] = synthesize_factor_coverage(
        plan=factor_coverage_plan,
        coverage_result=factor_coverage_result,
        claim_settlement=result.semantic_authority_result.settlement,
    ).to_dict()
    state["durable_transition_id"] = (
        result.compose_transition_id or result.authority_transition_id
    )
    state["durable_checkpoint"] = (
        result.compose_transition.to_dict()
        if result.compose_transition is not None
        else result.authority_transition.to_dict()
    )
    state["workflow_status"] = result.status
    return state


def _route_after_authority_settlement(state: WorkflowState) -> str:
    if state.get("workflow_status") != "authority_sealed":
        raise WorkflowFailure(
            "authority_settlement_terminal_invalid",
            failure_type="integrity",
        )
    if state.get("request", {}).get("stop_after_phase") == "phase04":
        return "stop"
    return "compose"


def _route_after_narrative_composition(state: WorkflowState) -> str:
    status = state.get("workflow_status")
    if status in {
        "narrative_failed",
        "publication_failed",
    }:
        return "stop"
    if status != "narrative_ready":
        raise WorkflowFailure(
            "narrative_composition_terminal_invalid",
            failure_type="integrity",
        )
    if state.get("request", {}).get("stop_after_phase") == "phase05":
        return "stop"
    return "deliver"


def _route_after_clarification_policy(state: WorkflowState) -> str:
    status = state["clarification_outcome"].get("boundary_status")
    if status == "needs_question":
        return "ask"
    if status == "clear":
        return "compile"
    raise WorkflowFailure(
        f"single_authority_clarification_route_invalid:{status or 'missing'}",
        failure_type="contract",
    )


def _invoke_llm(
    state: WorkflowState,
    task: str,
    payload: dict[str, Any],
    *,
    output_projector: (
        Callable[[Mapping[str, Any]], ContractProjection] | None
    ) = None,
    output_validator: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    spec = build_prompt(task, payload)
    try:
        call_kind, stage_name = _DURABLE_PROVIDER_STAGE_BY_TASK[task]
        journal = _provider_journal(state)
        intent_revision_id = None
        if call_kind != "intent_provider":
            intent_revision_id = str(state["intent_revision"]["intent_revision_id"])
        plan_revision_id = None
        raw_plan_revision = state.get("plan_revision")
        if isinstance(raw_plan_revision, Mapping):
            raw_plan_revision_id = raw_plan_revision.get("plan_revision_id")
            if isinstance(raw_plan_revision_id, str) and raw_plan_revision_id:
                plan_revision_id = raw_plan_revision_id
        client = DurableProviderClient(
            state["llm_client"],
            journal=journal,
            run_attempt_id=str(state["run_id"]),
            intent_revision_id=intent_revision_id,
            plan_revision_id=plan_revision_id,
            call_kind=call_kind,
            task_id=None,
            stage_name=stage_name,
        )
        invoke_kwargs: dict[str, Any] = {
            "task": spec.task,
            "prompt_version": spec.prompt_version,
            "messages": spec.messages,
            "required_keys": spec.required_keys,
        }
        if output_validator is not None:
            invoke_kwargs["output_validator"] = output_validator
        if output_projector is not None:
            invoke_kwargs["output_projector"] = output_projector
        model_tier, thinking = LLM_TASK_PROFILES.get(
            task,
            DEFAULT_LLM_TASK_PROFILE,
        )
        if bool(getattr(client, "supports_model_tier", False)):
            invoke_kwargs["model_tier"] = model_tier
        if bool(getattr(client, "supports_thinking_mode", False)):
            invoke_kwargs["thinking"] = thinking
        result = client.invoke_json(
            **invoke_kwargs,
        )
        accepted_attempt_refs = client.accepted_attempt_refs
        if len(accepted_attempt_refs) != 1:
            raise DurableCallJournalError("provider_stage_attempt_cardinality_invalid")
        state.setdefault("provider_attempt_refs", {})[stage_name] = (
            accepted_attempt_refs
        )
        state.setdefault("raw_provider_outputs", {})[stage_name] = dict(
            canonical_value(result.raw_output)
        )
    except WorkflowFailure:
        raise
    except Exception as exc:
        failure_audit = getattr(exc, "audit", None)
        if isinstance(failure_audit, Mapping) and failure_audit:
            state["llm_calls"].append(dict(failure_audit))
        raise WorkflowFailure(_exception_reason(exc), failure_type="llm") from exc
    state["llm_calls"].append(dict(canonical_value(result.audit)))
    return dict(canonical_value(result.output))


def _provider_journal(state: WorkflowState) -> DurableCallJournal:
    authority_store = state.get("request", {}).get("authority_store")
    journal = getattr(authority_store, "attempt_journal", None)
    if not isinstance(journal, DurableCallJournal):
        raise WorkflowFailure(
            "single_authority_provider_journal_missing",
            failure_type="contract",
        )
    return journal


def _provider_attempt_refs(
    state: WorkflowState,
    *,
    stage_name: str,
) -> tuple[str, ...]:
    refs = tuple((state.get("provider_attempt_refs") or {}).get(stage_name) or ())
    if len(refs) != 1:
        raise WorkflowFailure(
            "provider_stage_attempt_cardinality_invalid",
            failure_type="persistence",
        )
    return refs


def _validate_provider_stage_cardinality(
    state: WorkflowState,
    *,
    transition: DurableTransition,
    stage_name: str,
    expected_count: int,
) -> None:
    if type(expected_count) is not int or expected_count not in {0, 1}:
        raise WorkflowFailure(
            f"provider_stage_cardinality_contract_invalid:{stage_name}",
            failure_type="contract",
        )
    try:
        refs = _provider_journal(state).load_stage_attempt_refs(
            run_attempt_id=transition.run_attempt_id,
            transition_attempt_id=transition.attempt_id,
            stage_name=stage_name,
        )
    except (DurableCallJournalError, WorkflowFailure) as exc:
        raise WorkflowFailure(
            f"accepted_provider_stage_seal_invalid:{stage_name}",
            failure_type="persistence",
        ) from exc
    if len(refs) != expected_count:
        raise WorkflowFailure(
            f"accepted_provider_stage_seal_invalid:{stage_name}",
            failure_type="persistence",
        )


def _validate_provider_stage_seal(
    state: WorkflowState,
    *,
    transition: DurableTransition,
    stage_name: str,
) -> None:
    _validate_provider_stage_cardinality(
        state,
        transition=transition,
        stage_name=stage_name,
        expected_count=1,
    )


def _checkpoint(state: WorkflowState, node_name: str, attempt: int) -> dict[str, Any]:
    event = {
        "node": node_name,
        "attempt": attempt,
        "status": "running",
        "label": _BUSINESS_LABELS.get(node_name, node_name),
        "llm": node_name in _LLM_NODE_NAMES,
        "started_at": _utc_now(),
    }
    state["checkpoint_events"].append(event)
    return event


def _finish_checkpoint(event: dict[str, Any], status: str, started: float) -> None:
    event["status"] = status
    event["finished_at"] = _utc_now()
    event["duration_ms"] = round((perf_counter() - started) * 1000, 3)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_event(state: WorkflowState) -> dict[str, Any]:
    return state["checkpoint_events"][-1]


_LLM_NODE_NAMES = frozenset(
    {
        "understand_business_intent",
        "generate_clarification",
        "compile_authoritative_plan",
        "evaluate_claim_coverage",
        "compile_plan_patch",
    }
)

_BUSINESS_LABELS = {
    "understand_business_intent": "理解用户业务意图",
    "decide_question_boundary": "判断问题边界是否清楚",
    "clarification_policy_gate": "澄清策略门禁",
    "generate_clarification": "生成澄清问题",
    "persist_clarification": "保存待确认的业务决定",
    "compile_authoritative_plan": "编译唯一权威分析计划",
    "execute_capability_dag": "执行权威分析能力并固化证据",
    "evaluate_claim_coverage": "检查结论证据覆盖",
    "compile_plan_patch": "编译增量权威分析计划",
}
