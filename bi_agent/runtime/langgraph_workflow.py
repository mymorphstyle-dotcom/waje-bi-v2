from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from time import perf_counter
import warnings
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, TypedDict
from zoneinfo import ZoneInfo

from langchain_core._api.deprecation import LangChainPendingDeprecationWarning

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
)
warnings.filterwarnings(
    "ignore",
    category=LangChainPendingDeprecationWarning,
)
from langgraph.graph import END, StateGraph

from bi_agent.conversation.clarification_authority import (
    build_execution_material,
    build_material_authority,
    validate_prior_topic_material_context,
)
from bi_agent.conversation.clarification_options import (
    clarification_labels_match,
    project_clarification_recommendation,
)
from bi_agent.conversation.models import CLARIFICATION_ESCAPE_OPTION
from bi_agent.capabilities.candidate_dimension_screen import candidate_dimension_screen
from bi_agent.capabilities.data_quality_check import data_quality_check
from bi_agent.capabilities.driver_decomposition import driver_decomposition
from bi_agent.capabilities.formula_decompose import formula_decompose
from bi_agent.capabilities.high_value_user_contribution import high_value_user_contribution
from bi_agent.capabilities.joint_attribution import joint_attribution
from bi_agent.capabilities.outlier_scan import outlier_scan
from bi_agent.capabilities.outlier_contribution import outlier_contribution
from bi_agent.capabilities.pattern_scan import scan_pattern
from bi_agent.capabilities.segment_contribution import segment_contribution
from bi_agent.capabilities.segment_bridge import segment_bridge
from bi_agent.capabilities.user_mix_contribution import user_mix_contribution
from bi_agent.runtime.answer_package import (
    build_answer_package,
    reverify_answer_package_for_delivery,
)
from bi_agent.runtime.analysis_assets import material_assumption_digest
from bi_agent.runtime.analysis_contracts import (
    analysis_contract_from_dict,
    analysis_contract_signature,
)
from bi_agent.runtime.analysis_runtime import (
    AnswerPackageBuildContext,
    AnalysisRuntimeRequest,
    analysis_outcome_requires_route_clarification,
)
from bi_agent.runtime.artifacts import persist_artifact, to_jsonable
from bi_agent.runtime.baseline_semantics import (
    BaselineSemanticError,
    CANONICAL_BASELINE_IDS,
    baseline_llm_semantics,
    canonical_baseline_ids,
)
from bi_agent.runtime.capability_harness import (
    PATTERN_COMPARE_CAPABILITIES,
    WINDOW_METRIC_COMPARE_CAPABILITIES,
    execute_capability,
)
from bi_agent.runtime.capability_models import CapabilityRequest
from bi_agent.runtime.capability_execution import (
    BoundCapabilityInput,
    capability_binding_claim_ready,
    validate_bound_capability_input,
)
from bi_agent.runtime.capability_registry import llm_capability_cards
from bi_agent.runtime.compiler import compile_graph
from bi_agent.runtime.analysis_obligations import (
    ObligationRequest,
    capability_dataset_requirements,
    obligation_condition_matches,
    resolve_partitioned_analysis_obligations,
)
from bi_agent.runtime.data_contract_diagnostics import (
    contract_fields_from_records,
    diagnose_contract_gaps,
)
from bi_agent.runtime.diagnostic_insights import (
    build_diagnostic_insight_portfolio,
    cross_source_auxiliary_claim_text,
)
from bi_agent.runtime.exploration_budget import default_budget, record_capability_call
from bi_agent.runtime.formula_candidates import (
    build_formula_candidate_framework,
)
from bi_agent.runtime.final_narrative_binding import (
    build_authority_safe_narrative,
    build_narrative_authority_record,
    build_narrative_question_scope,
)
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)
from bi_agent.runtime.llm_client import (
    LLMOutputError,
    OpenAICompatibleLLMClient,
    _localize_narrative_fields,
)
from bi_agent.runtime.llm_prompts import (
    BUSINESS_INTENT_PATTERN_FAMILIES,
    build_prompt,
)
from bi_agent.runtime.models import (
    CompiledGraph,
    GraphNode,
    MutationLedger,
    MutationRecord,
)
from bi_agent.runtime.sql_safety import validate_select_only
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.runtime.window_resolver import CURRENT_DATA_BASELINES


LLM_REQUIRED_TASKS = (
    "business_intent",
    "boundary_decision",
    "confirm_understanding",
    "analysis_route_plan",
    "final_route_narrative",
    "data_coverage_interpretation",
    "next_action",
    "evidence_interpretation",
    "causal_audit",
    "answer_synthesis",
    "semantic_audit",
)
DEFAULT_LLM_TASK_PROFILE = ("default", "disabled")
LLM_TASK_PROFILES: dict[str, tuple[str, str]] = {
    "business_intent": ("default", "enabled"),
    "semantic_audit": ("default", "enabled"),
    "analysis_route_plan": ("critical", "disabled"),
    "route_repair": ("critical", "disabled"),
    "next_action": ("critical", "disabled"),
    "promotion_direction": ("critical", "disabled"),
    "evidence_interpretation": ("default", "disabled"),
    "answer_synthesis": ("critical", "enabled"),
    "final_business_summary": ("critical", "enabled"),
    "final_narrative_binding": ("default", "disabled"),
}
_PRIOR_TOPIC_PRIVATE_MATERIAL_AXES = (
    "question_family",
    "pattern_family",
    "pattern_params",
    "target_claim",
    "target",
    "target_metric",
    "scope",
    "time_window",
    "baseline",
    "baseline_candidates",
)
_SUPPORTED_PATTERN_FAMILIES = frozenset(BUSINESS_INTENT_PATTERN_FAMILIES)
ROUTE_BLOCKED_CAPABILITY_IDS = frozenset(
    {
        "evidence_reduce",
        "metric_coverage_profile",
        "metric_timeseries",
        "component_contribution",
        "segment_breakdown",
        "segment_shift_compare",
        "change_point_scan",
    }
)
MATERIAL_AUTHORITY_LIST_AXES = (
    "target_metrics",
    "component_ids",
    "association_metric_ids",
    "dimension_ids",
    "baselines",
    "context_sources",
    "claim_types",
    "required_outcomes",
    "analysis_axis_ids",
)
_LOCAL_OBLIGATION_REJECTION_REASONS = frozenset(
    {
        "diagnostic_question_family_incompatible",
        "unknown_diagnostic_rejected",
    }
)
_ROUTE_CAPABILITY_SECTION_FIELDS = frozenset(
    {"route_step", "expected_evidence"}
)
_ANALYSIS_ROUTE_PROVIDER_FIELDS = frozenset(
    {
        "requested_nodes",
        "route_summary",
        "expected_evidence",
        "capability_sections",
        "analysis_requirements",
        "decision_summary",
        "display_summary",
    }
)
_ROUTE_REPAIR_PROVIDER_FIELDS = frozenset(
    {
        "requested_nodes",
        "analysis_requirements",
        "repair_summary",
        "decision_summary",
        "display_summary",
    }
)
_ROUTE_NARRATIVE_HARD_AUTHORITY_FIELDS = (
    "requested_nodes",
    "analysis_requirements",
    "outcome_resolution",
    "obligation_resolution",
    "accepted_degradation_choice",
    "capability_sections.keys",
    "expected_evidence.keys",
)
_ROUTE_NARRATIVE_ADVISORY_FIELDS = (
    "route_overview",
    "route_summary",
    "decision_summary",
    "display_summary",
    "capability_sections.*.route_step",
    "capability_sections.*.expected_evidence",
    "expected_evidence.values",
)


class WorkflowState(TypedDict, total=False):
    request: dict[str, Any]
    accepted_assumptions: list[dict[str, Any]]
    run_id: str
    checkpoint_events: list[dict[str, Any]]
    validator_results: list[dict[str, Any]]
    llm_calls: list[dict[str, Any]]
    llm_client: Any
    budget_state: Any
    intent: dict[str, Any]
    boundary_decision: dict[str, Any]
    route_material_conflicts: tuple[str, ...]
    obligation_rejection_history: tuple[dict[str, str], ...]
    clarification_outcome: dict[str, Any]
    clarification_choice_consumed: bool
    confirmed_understanding: dict[str, Any]
    analysis_route: dict[str, Any]
    repair_attempts: int
    answer_repair_attempts: int
    semantic_repair_attempts: int
    verifier_repair_attempts: int
    compiled_graph: Any
    schema: dict[str, Any]
    sql_text: str
    sql_hash: str
    row_query_plan: dict[str, Any]
    runtime_rows_by_intent: dict[str, list[dict[str, Any]]]
    coverage_interpretation: dict[str, Any]
    evidence: list[dict[str, Any]]
    evidence_brief: dict[str, Any]
    diagnostic_insights: dict[str, Any]
    diagnostic_route_history: list[str]
    next_action: dict[str, Any]
    evidence_interpretation: dict[str, Any]
    causal_evidence_dossier: dict[str, Any]
    causal_audit: dict[str, Any]
    answer_text: str
    draft_claims: list[dict[str, Any]]
    authority_verified_claims: list[dict[str, Any]]
    authority_verified_claims_digest: str
    semantic_audit: dict[str, Any]
    verifier: dict[str, Any]
    retry_context: dict[str, Any]
    final_explanation: dict[str, Any]
    final_business_summary: str
    rejected_final_business_summary: str
    final_narrative_statement_bindings: list[dict[str, Any]]
    final_summary_publication_repair: dict[str, Any]
    final_answer_audit: dict[str, Any]
    final_summary_display_warnings: list[str]
    quality_gate: dict[str, Any]
    follow_up_questions: list[str]
    answer_package: dict[str, Any]
    artifact_path: str
    contract_gap_diagnostics: tuple[dict[str, Any], ...]
    analysis_compile_outcome: Any
    analysis_runtime_result: Any
    execution_material: dict[str, Any]
    query_repair_decisions: tuple[dict[str, Any], ...]
    query_gap_clarification: dict[str, Any]
    workflow_status: str
    workflow_failure_reason: str


@dataclass(frozen=True)
class WorkflowRunResult:
    status: str
    run_id: str
    answer_package: Optional[dict[str, Any]] = None
    artifact_path: str = ""
    failure_reason: str = ""
    checkpoint_events: tuple[dict[str, Any], ...] = ()
    llm_calls: tuple[dict[str, Any], ...] = ()
    analysis_runtime_records: Optional[Mapping[str, Any]] = None
    analysis_runtime_result: Any = None
    completed_material_authority: Optional[Mapping[str, Any]] = None


class WorkflowFailure(Exception):
    def __init__(self, message: str, *, failure_type: str = "technical"):
        super().__init__(message)
        self.failure_type = failure_type


def _exception_reason(exc: BaseException) -> str:
    return str(exc).strip() or type(exc).__name__


def run_pattern_workflow(request: Optional[dict[str, Any]] = None) -> WorkflowRunResult:
    request = dict(request or {})
    request.setdefault("run_mode", "production")
    if not isinstance(request["run_mode"], str) or request["run_mode"] not in {
        "production",
        "live",
    }:
        return WorkflowRunResult(
            status="failed",
            run_id=str(request.get("run_id") or "phase4-draft"),
            failure_reason="analysis_runtime_run_mode_invalid",
        )
    context_manifest = request.get("context_manifest") or {}
    manifest_assumption = next(
        (
            item
            for item in context_manifest.get("accepted_assumptions") or ()
            if isinstance(item, Mapping)
        ),
        {},
    )
    if manifest_assumption and not request.get("accepted_degradation_choice"):
        request["accepted_degradation_choice"] = dict(manifest_assumption)
    accepted_choice = request.get("accepted_degradation_choice") or {}
    state: WorkflowState = {
        "request": request,
        "accepted_assumptions": (
            [dict(accepted_choice)]
            if isinstance(accepted_choice, Mapping) and accepted_choice
            else []
        ),
        "run_id": request.get("run_id") or "phase4-draft",
        "checkpoint_events": [],
        "validator_results": [],
        "llm_calls": [],
        "repair_attempts": 0,
        "answer_repair_attempts": 0,
        "semantic_repair_attempts": 0,
        "verifier_repair_attempts": 0,
    }
    if "llm_client" in request:
        state["llm_client"] = request["llm_client"]
    elif "llm_client" in request.get("runtime", {}):
        state["llm_client"] = request["runtime"]["llm_client"]
    else:
        try:
            state["llm_client"] = OpenAICompatibleLLMClient.from_env()
        except Exception as exc:
            return WorkflowRunResult(
                status="failed",
                run_id=state["run_id"],
                failure_reason=f"llm_binding_failed:{_exception_reason(exc)}",
                checkpoint_events=tuple(state["checkpoint_events"]),
                llm_calls=tuple(state["llm_calls"]),
            )

    try:
        output = build_pattern_graph().invoke(
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

    completed_material_authority = None
    if (
        str(output.get("workflow_status") or "draft") == "draft"
        and isinstance(output.get("execution_material"), Mapping)
        and str(request.get("thread_id") or "")
        and str(request.get("topic_id") or "")
    ):
        route = output.get("analysis_route") or {}
        resolution = (
            route.get("obligation_resolution") or {}
            if isinstance(route, Mapping)
            else {}
        )
        try:
            completed_material_authority = build_material_authority(
                source_run_id=str(output["run_id"]),
                thread_id=str(request["thread_id"]),
                topic_id=str(request["topic_id"]),
                original_intent=output.get("intent") or {},
                material_slots=_clarification_material_slots(output),
                runtime_material=output["execution_material"],
                obligation_rejection_history=(
                    resolution.get("mutation_history") or ()
                    if isinstance(resolution, Mapping)
                    else ()
                ),
            )
        except Exception as exc:
            return WorkflowRunResult(
                status="failed",
                run_id=str(output["run_id"]),
                failure_reason=(
                    "completed_material_authority_build_failed:"
                    f"{_exception_reason(exc)}"
                ),
                checkpoint_events=tuple(
                    output.get("checkpoint_events") or ()
                ),
                llm_calls=tuple(output.get("llm_calls") or ()),
            )
    return WorkflowRunResult(
        status=str(output.get("workflow_status") or "draft"),
        run_id=output["run_id"],
        answer_package=output.get("answer_package"),
        artifact_path=str(output.get("artifact_path") or ""),
        failure_reason=str(output.get("workflow_failure_reason") or ""),
        checkpoint_events=tuple(output["checkpoint_events"]),
        llm_calls=tuple(output.get("llm_calls") or ()),
        analysis_runtime_records=(
            output.get("analysis_runtime_result").persistence_records
            if output.get("analysis_runtime_result") is not None
            else None
        ),
        analysis_runtime_result=output.get("analysis_runtime_result"),
        completed_material_authority=completed_material_authority,
    )


def build_pattern_graph():
    graph = StateGraph(WorkflowState)
    for node, func in (
        ("understand_business_intent", _understand_business_intent),
        ("decide_question_boundary", _decide_question_boundary),
        ("clarification_policy_gate", _clarification_policy_gate),
        ("generate_clarification", _generate_clarification),
        ("persist_clarification", _persist_clarification),
        ("rebind_after_clarification", _rebind_after_clarification),
        ("confirm_business_understanding", _confirm_business_understanding),
        ("design_analysis_route", _design_analysis_route),
        ("accept_analysis_route", _accept_analysis_route),
        ("repair_analysis_route", _repair_analysis_route),
        ("inspect_schema", _inspect_schema),
        ("validate_runtime_binding", _validate_runtime_binding),
        ("fetch_runtime_rows", _fetch_runtime_rows),
        ("validate_query_completeness", _validate_query_completeness),
        ("decide_query_repair", _decide_query_repair),
        ("repair_analysis_contract", _repair_analysis_contract),
        ("generate_query_gap_clarification", _generate_query_gap_clarification),
        ("persist_query_gap_clarification", _persist_query_gap_clarification),
        ("interpret_data_coverage", _interpret_data_coverage),
        ("execute_capabilities", _execute_capabilities),
        ("reduce_evidence", _reduce_evidence),
        ("build_diagnostic_insights", _build_diagnostic_insights),
        ("decide_next_action", _decide_next_action),
        ("promotion_direction", _promotion_direction),
        ("promotion_policy_gate", _promotion_policy_gate),
        ("execute_joint_attribution", _execute_joint_attribution),
        ("interpret_evidence", _interpret_evidence),
        ("audit_causal_implications", _audit_causal_implications),
        ("synthesize_answer", _synthesize_answer),
        ("semantic_audit", _semantic_audit),
        ("hard_verify_answer", _hard_verify_answer),
        ("repair_answer", _repair_answer),
        ("generate_degraded_explanation", _generate_degraded_explanation),
        ("generate_blocked_explanation", _generate_blocked_explanation),
        ("final_business_summary", _final_business_summary),
        ("answer_quality_gate", _answer_quality_gate),
        ("persist_artifact", _persist_artifact),
    ):
        graph.add_node(node, _retrying_node(node, func))

    graph.set_entry_point("understand_business_intent")
    graph.add_edge("understand_business_intent", "decide_question_boundary")
    graph.add_edge("decide_question_boundary", "clarification_policy_gate")
    graph.add_conditional_edges(
        "clarification_policy_gate",
        _route_after_clarification_policy,
        {
            "confirm": "confirm_business_understanding",
            "rebind": "rebind_after_clarification",
            "ask": "generate_clarification",
            "block": "generate_blocked_explanation",
        },
    )
    graph.add_conditional_edges(
        "generate_clarification",
        _route_after_clarification,
        {
            "rebind": "rebind_after_clarification",
            "wait": "persist_clarification",
            "block": "generate_blocked_explanation",
        },
    )
    graph.add_edge("persist_clarification", END)
    graph.add_edge("rebind_after_clarification", "decide_question_boundary")
    graph.add_edge("confirm_business_understanding", "design_analysis_route")
    graph.add_edge("design_analysis_route", "accept_analysis_route")
    graph.add_conditional_edges(
        "accept_analysis_route",
        _route_after_accept_analysis,
        {
            "accepted": "inspect_schema",
            "repair": "repair_analysis_route",
            "ask": "generate_clarification",
            "degrade": "generate_degraded_explanation",
            "block": "generate_blocked_explanation",
        },
    )
    graph.add_edge("repair_analysis_route", "accept_analysis_route")
    graph.add_edge("inspect_schema", "validate_runtime_binding")
    graph.add_conditional_edges(
        "validate_runtime_binding",
        _route_after_runtime_binding,
        {
            "valid": "fetch_runtime_rows",
            "block": "generate_blocked_explanation",
        },
    )
    graph.add_edge("fetch_runtime_rows", "validate_query_completeness")
    graph.add_edge("validate_query_completeness", "decide_query_repair")
    graph.add_conditional_edges(
        "decide_query_repair",
        _route_after_query_repair,
        {
            "ready": "interpret_data_coverage",
            "degraded": "interpret_data_coverage",
            "recompile": "repair_analysis_contract",
            "clarify": "generate_query_gap_clarification",
            "block": "generate_blocked_explanation",
        },
    )
    graph.add_edge("repair_analysis_contract", "fetch_runtime_rows")
    graph.add_conditional_edges(
        "generate_query_gap_clarification",
        _route_after_query_gap_clarification,
        {
            "wait": "persist_query_gap_clarification",
            "block": "generate_blocked_explanation",
        },
    )
    graph.add_edge("persist_query_gap_clarification", END)
    graph.add_conditional_edges(
        "interpret_data_coverage",
        _route_after_coverage,
        {
            "sufficient": "execute_capabilities",
            "ask": "generate_clarification",
            "degrade": "generate_degraded_explanation",
            "block": "generate_blocked_explanation",
        },
    )
    graph.add_edge("execute_capabilities", "reduce_evidence")
    graph.add_edge("reduce_evidence", "build_diagnostic_insights")
    graph.add_edge("build_diagnostic_insights", "decide_next_action")
    graph.add_conditional_edges(
        "decide_next_action",
        _route_after_next_action,
        {
            "plan": "design_analysis_route",
            "ask": "generate_clarification",
            "promote": "promotion_direction",
            "synthesize": "interpret_evidence",
            "degrade": "generate_degraded_explanation",
        },
    )
    graph.add_edge("promotion_direction", "promotion_policy_gate")
    graph.add_conditional_edges(
        "promotion_policy_gate",
        _route_after_promotion_policy,
        {
            "accepted": "execute_joint_attribution",
            "synthesize": "interpret_evidence",
            "ask": "generate_clarification",
            "degrade": "generate_degraded_explanation",
            "block": "generate_blocked_explanation",
        },
    )
    graph.add_edge("execute_joint_attribution", "reduce_evidence")
    graph.add_edge("interpret_evidence", "audit_causal_implications")
    graph.add_edge("audit_causal_implications", "synthesize_answer")
    graph.add_edge("synthesize_answer", "semantic_audit")
    graph.add_conditional_edges(
        "semantic_audit",
        _route_after_semantic_audit,
        {
            "verify": "hard_verify_answer",
            "repair": "repair_answer",
        },
    )
    graph.add_conditional_edges(
        "hard_verify_answer",
        _route_after_hard_verify,
        {
            "passed": "final_business_summary",
            "repair": "repair_answer",
            "ask": "generate_clarification",
            "degrade": "generate_degraded_explanation",
        },
    )
    graph.add_edge("repair_answer", "semantic_audit")
    graph.add_edge("generate_degraded_explanation", "final_business_summary")
    graph.add_edge("generate_blocked_explanation", "final_business_summary")
    graph.add_edge("final_business_summary", "answer_quality_gate")
    graph.add_edge("answer_quality_gate", "persist_artifact")
    graph.add_edge("persist_artifact", END)
    return graph.compile()


def _retrying_node(node_name, func):
    def run(state: WorkflowState) -> WorkflowState:
        started = perf_counter()
        event = _checkpoint(state, node_name, 1)
        try:
            result = func(state)
            _finish_checkpoint(event, "completed", started)
            if node_name == "persist_artifact":
                _refresh_persisted_answer_package(result)
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


def _understand_business_intent(state: WorkflowState) -> WorkflowState:
    request = state["request"]
    if request.get("force_langgraph_failure"):
        raise RuntimeError("forced_langgraph_failure")
    _maybe_force_node_failure(state, "understand_business_intent")
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    intent_payload = _business_intent_payload(
        {
            **request,
            "_retry_selected_families": state.get(
                "last_business_intent_families", ()
            ),
        },
        registry=registry,
    )
    output = _invoke_llm(
        state,
        "business_intent",
        intent_payload,
        output_validator=lambda candidate: _validate_business_intent_provider_output(
            candidate,
            request,
            registry,
        ),
    )
    answer_contract = output.get("answer_contract", {})
    if not isinstance(answer_contract, Mapping):
        raise WorkflowFailure(
            "business_intent_contract_invalid:answer_contract",
            failure_type="llm_contract",
        )
    material = _material_business_intent_values(request, output, registry)
    pattern_family = str(material["pattern_family"])
    pattern_params = _normalize_pattern_params(
        request,
        output,
        pattern_family,
        allow_question_inference=False,
        require_output_mapping=True,
    )
    if pattern_family == "weekly" and not _weekly_pattern_has_weekday_target(
        pattern_params
    ):
        raise WorkflowFailure(
            "business_intent_contract_invalid:pattern_params",
            failure_type="llm_contract",
        )
    if pattern_family == "intra_period" and not _intra_period_has_target(
        pattern_params
    ):
        raise WorkflowFailure(
            "business_intent_contract_invalid:pattern_params",
            failure_type="llm_contract",
        )
    material_requirements = _validated_business_intent_requirements(
        output.get("analysis_requirements"),
        registry,
    )
    baseline_candidates = _validated_business_intent_baseline_candidates(
        output.get("baseline_candidates"),
        production_like=True,
    )
    sub_intents = _validated_business_intent_sequence(
        output.get("sub_intents"),
        field="sub_intents",
    )
    ambiguous_slots = _validated_business_intent_sequence(
        output.get("ambiguous_slots"),
        field="ambiguous_slots",
    )
    intent = _normalize_question_families({
        "question_family": material["question_family"],
        "target_metric": material["target_metric"],
        "pattern_family": pattern_family,
        "pattern_params": pattern_params,
        "scope": material["scope"],
        "time_window": material["time_window"],
        "target_claim": material["target_claim"],
        "baseline_candidates": baseline_candidates,
        "sub_intents": sub_intents,
        "ambiguous_slots": ambiguous_slots,
        "answer_contract": dict(answer_contract),
        "baseline": request.get("baseline") or output.get("baseline") or {},
        "target": request.get("target") or output.get("target") or {},
        "question": request.get("question", ""),
        "requested_nodes": (),
        "question_families": output.get("question_families") or (),
        "primary_question_family": output.get("primary_question_family"),
        "secondary_question_families": output.get("secondary_question_families") or (),
        **material_requirements,
    })
    state["last_business_intent_families"] = list(
        _intent_question_family_set(intent)
    )
    intent = _bind_clarification_attempt_intent(intent, request, registry)
    intent = _write_back_resolved_target_date(intent, request, registry)
    intent = _write_back_baseline_binding(intent, request)
    intent = _bind_one_day_comparison_pattern(intent)
    intent = _bind_compiled_analysis_plan(intent, registry)
    state["obligation_rejection_history"] = tuple(
        intent.pop("_validated_obligation_rejection_history", ())
    )
    _validate_context_family_axis(intent, registry)
    state["intent"] = intent
    return state


_CANONICAL_RELATIVE_TARGET_IDS = ("yesterday",)
_CANONICAL_RELATIVE_TARGET_ID_SET = frozenset(
    _CANONICAL_RELATIVE_TARGET_IDS
)
_CLAIM_INTENT_BUSINESS_SEMANTICS = {
    "observed_activity": (
        "用户明确询问的活动、玩法、运营事件或业务对象在分析窗口中的客观观测"
    ),
    "comparative_change": "目标窗口相对已确认基线的数值和方向变化",
    "contract_coverage_and_trust_boundary": (
        "数据合同、覆盖范围、数据可用性和结论可信边界"
    ),
    "source_reconciliation": "多个数据来源之间的口径与数值对账",
    "baseline_stability": "目标表现相对多个或滚动基线的稳定程度",
    "formula_component_contribution": (
        "公式组成指标对目标指标变化的量化贡献"
    ),
    "segment_contribution_or_mix_shift": (
        "业务分群的贡献以及分群结构变化"
    ),
    "recurring_pattern_existence": "跨多个周期重复出现的业务模式",
    "candidate_mechanism": "有证据约束但仍需进一步验证的作用机制",
    "candidate_driver": "有证据约束但仍需进一步验证的驱动因素",
    "cross_source_statistical_association": (
        "两个已对齐业务序列之间经过稳定性、滞后和多重检验约束的统计关联"
    ),
    "concentration": "变化或业务量在少数对象中的集中程度",
    "external_shock_candidate_or_anomaly": (
        "外部冲击或异常事件的候选解释"
    ),
    "mix_shift": "业务构成占比变化",
    "contribution": "分析对象对总体变化的量化贡献",
    "business_object_candidate_impact": (
        "活动、事件、渠道或其他业务对象的候选影响"
    ),
}
_STRUCTURED_TARGET_TIME_KEYS = frozenset(
    {"target", "target_date", "date", "time_window"}
)
_STRUCTURED_DATE_BOUND_KEYS = frozenset(
    {"start", "end", "start_inclusive", "end_exclusive"}
)


def _bind_compiled_analysis_plan(
    intent: Mapping[str, Any],
    registry: RuntimeContractRegistry,
) -> dict[str, Any]:
    """Bind the complete reviewed analysis universe after LLM goal parsing."""

    goal_bindings = intent.get("goal_bindings") or ()
    explicit_focus = intent.get("explicit_focus") or {}
    try:
        plan = registry.compile_goal_analysis_plan(
            goal_bindings=goal_bindings,
            target_metric=str(intent.get("target_metric") or ""),
            explicit_focus=explicit_focus,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowFailure(
            f"business_intent_analysis_plan_invalid:{_exception_reason(exc)}",
            failure_type="contract",
        ) from exc
    axes = plan.get("analysis_axes") or ()
    dimensions = tuple(
        dict.fromkeys(
            str(dimension_id)
            for axis in axes
            if isinstance(axis, Mapping)
            for dimension_id in axis.get("dimension_refs") or ()
            if str(dimension_id)
        )
    )
    components = tuple(
        dict.fromkeys(
            str(metric_id)
            for axis in axes
            if isinstance(axis, Mapping)
            and str(axis.get("axis_kind") or "") == "formula_tree"
            for metric_id in axis.get("metric_refs") or ()
            if str(metric_id) and str(metric_id) != intent.get("target_metric")
        )
    )
    association_metrics = tuple(
        dict.fromkeys(
            str(metric_id)
            for axis in axes
            if isinstance(axis, Mapping)
            and str(axis.get("axis_kind") or "") == "cross_source_context"
            for metric_id in axis.get("metric_refs") or ()
            if str(metric_id) and str(metric_id) != intent.get("target_metric")
        )
    )
    context_sources = tuple(
        dict.fromkeys(
            str(source_id)
            for source_id in explicit_focus.get("context_source_ids") or ()
            if str(source_id)
        )
    )
    claim_types_by_role: dict[str, list[str]] = {
        "required": [],
        "auxiliary": [],
        "conditional": [],
    }
    for axis in axes:
        if not isinstance(axis, Mapping):
            continue
        role = str(axis.get("role") or "")
        if role not in claim_types_by_role:
            continue
        for capability_id in axis.get("capability_refs") or ():
            try:
                contract = registry.capability_inputs(str(capability_id))
            except KeyError:
                continue
            claim_types_by_role[role].extend(
                str(claim_type)
                for claim_type in contract.get("supported_claim_types") or ()
                if str(claim_type)
            )
    required_claim_types = tuple(
        dict.fromkeys(claim_types_by_role["required"])
    )
    auxiliary_claim_types = tuple(
        claim_type
        for claim_type in dict.fromkeys(
            (*claim_types_by_role["auxiliary"], *claim_types_by_role["conditional"])
        )
        if claim_type not in set(required_claim_types)
    )
    return {
        **dict(intent),
        "analysis_plan": to_jsonable(plan),
        "required_outcomes": list(plan.get("required_outcomes") or ()),
        "analysis_axes": to_jsonable(axes),
        "analysis_axis_ids": [
            str(axis.get("axis_id") or "")
            for axis in axes
            if isinstance(axis, Mapping) and str(axis.get("axis_id") or "")
        ],
        "dimension_ids": list(dimensions),
        "component_ids": list(components),
        "association_metric_ids": list(association_metrics),
        "context_sources": list(context_sources),
        "required_claim_types": list(required_claim_types),
        "auxiliary_claim_types": list(auxiliary_claim_types),
        "publishable_claim_types": list(
            dict.fromkeys((*required_claim_types, *auxiliary_claim_types))
        ),
    }

_EXPLICIT_BASELINE_TOKENS = (
    (
        "same_weekday_last_week",
        ("上周同日", "上周这一天"),
    ),
    (
        "rolling_7_day_baseline",
        (
            "近7日均值",
            "近7天均值",
            "近七日均值",
            "近七天均值",
            "过去7日均值",
            "过去7天均值",
            "7日均值",
            "7天均值",
        ),
    ),
    (
        "previous_day",
        ("前一天", "前一日", "前天", "上一个自然日"),
    ),
)


def _write_back_baseline_binding(
    intent: Mapping[str, Any],
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Record whether a baseline is user-bound or only model-suggested."""

    output = dict(intent)
    candidates = _ordered_baseline_ids(output.get("baseline_candidates"))
    source = "provider_suggestion" if candidates else "unbound"
    confirmed = False

    choice = request.get("clarification_choice") or {}
    choice_candidates = (
        _ordered_baseline_ids(choice.get("baseline_candidates"))
        if isinstance(choice, Mapping)
        else []
    )
    if choice_candidates:
        candidates = choice_candidates
        source = "user_clarification"
        confirmed = True
    else:
        resume = request.get("clarification_attempt_context") or {}
        prior_intent = (
            resume.get("original_intent")
            if isinstance(resume, Mapping)
            else {}
        )
        prior_binding = (
            prior_intent.get("baseline_binding")
            if isinstance(prior_intent, Mapping)
            else {}
        )
        prior_candidates = (
            _ordered_baseline_ids(
                prior_binding.get("candidates")
                or prior_intent.get("baseline_candidates")
            )
            if isinstance(prior_binding, Mapping)
            and bool(prior_binding.get("confirmed"))
            else []
        )
        request_candidates = _ordered_baseline_ids(
            request.get("baseline_candidates")
        )
        explicit_question_candidates = _explicit_question_baselines(
            str(request.get("question") or output.get("question") or "")
        )
        if prior_candidates:
            candidates = prior_candidates
            source = "resume_material_authority"
            confirmed = True
        elif request_candidates or request.get("baseline"):
            candidates = request_candidates or candidates
            source = "request_contract"
            confirmed = bool(candidates)
        elif len(explicit_question_candidates) == 1:
            candidates = explicit_question_candidates
            source = "user_question"
            confirmed = True

    output["baseline_candidates"] = list(candidates)
    output["baseline_binding"] = {
        "confirmed": confirmed,
        "source": source,
        "candidates": list(candidates),
    }
    if confirmed:
        output["ambiguous_slots"] = [
            item
            for item in output.get("ambiguous_slots") or ()
            if str(item.get("slot") if isinstance(item, Mapping) else item)
            not in {"baseline", "baselines"}
        ]
    return output


def _bind_one_day_comparison_pattern(
    intent: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep one-day change questions anchored to their primary comparison."""

    output = dict(intent)
    target = str(output.get("time_window") or "").strip()
    try:
        exact_day = datetime.fromisoformat(target).date().isoformat() == target
    except ValueError:
        exact_day = False
    if not exact_day:
        return output

    families = _intent_question_family_set(output)
    goals = {
        str(binding.get("goal_id") or "")
        for binding in output.get("goal_bindings") or ()
        if isinstance(binding, Mapping)
    }
    if not families.intersection(
        {"paid_amount_change_explanation", "custom_baseline_comparison"}
    ) or not goals.intersection({"explain_change", "validate_change", "compare_baseline"}):
        return output

    raw_params = output.get("pattern_params") or {}
    raw_params = raw_params if isinstance(raw_params, Mapping) else {}
    comparison_param_keys = {
        "period_key",
        "group_key",
        "target_group",
        "baseline_group",
        "min_periods",
        "materiality_floor",
    }
    params = {
        key: value
        for key, value in raw_params.items()
        if key in comparison_param_keys
    }
    params.setdefault("group_key", "group")
    params.setdefault("target_group", "target")
    params["baseline_group"] = "baseline"
    output["pattern_family"] = "custom_baseline"
    output["pattern_params"] = params
    return output


def _ordered_baseline_ids(raw: Any) -> list[str]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return _canonical_baseline_ids(raw)
    ordered: list[str] = []
    for item in raw:
        for baseline_id in _canonical_baseline_ids([item]):
            if baseline_id not in ordered:
                ordered.append(baseline_id)
    return ordered


def _explicit_question_baselines(question: str) -> list[str]:
    compact = re.sub(r"\s+", "", question)
    matches = [
        baseline_id
        for baseline_id, tokens in _EXPLICIT_BASELINE_TOKENS
        if any(token in compact for token in tokens)
    ]
    return list(dict.fromkeys(matches))


def _write_back_resolved_target_date(
    intent: Mapping[str, Any],
    request: dict[str, Any],
    registry: RuntimeContractRegistry,
) -> dict[str, Any]:
    """Bind relative daily targets to one auditable run clock."""

    time_window = intent.get("time_window")
    raw_target = time_window
    if isinstance(time_window, Mapping):
        raw_target = next(
            (
                time_window[key]
                for key in ("target", "target_date", "date", "time_window")
                if key in time_window
            ),
            "",
        )
    if not isinstance(raw_target, str):
        return dict(intent)
    target_token = raw_target.strip()
    if not target_token:
        return dict(intent)
    relative_yesterday = (
        target_token.lower() in _CANONICAL_RELATIVE_TARGET_ID_SET
    )
    explicit_target = ""
    if not relative_yesterday:
        try:
            explicit_target = datetime.fromisoformat(target_token).date().isoformat()
        except ValueError:
            return dict(intent)
        if explicit_target != target_token:
            return dict(intent)

    raw_context = request.get("analysis_context") or {}
    if not isinstance(raw_context, Mapping):
        raise WorkflowFailure(
            "business_intent_analysis_context_invalid",
            failure_type="contract",
        )
    analysis_context = dict(raw_context)
    raw_as_of = analysis_context.get("as_of")
    if raw_as_of:
        try:
            as_of = (
                datetime.fromisoformat(raw_as_of)
                if isinstance(raw_as_of, str)
                else raw_as_of
            )
        except ValueError as exc:
            raise WorkflowFailure(
                "business_intent_analysis_context_invalid:as_of",
                failure_type="contract",
            ) from exc
        if not isinstance(as_of, datetime) or as_of.tzinfo is None:
            raise WorkflowFailure(
                "business_intent_analysis_context_invalid:as_of",
                failure_type="contract",
            )
        canonical_as_of = (
            raw_as_of if isinstance(raw_as_of, str) else as_of.isoformat()
        )
    else:
        as_of = datetime.now(timezone.utc)
        canonical_as_of = as_of.isoformat()
    target_date = explicit_target or (
        as_of.astimezone(ZoneInfo(registry.business_timezone)).date()
        - timedelta(days=1)
    ).isoformat()
    analysis_context.update(
        {
            "as_of": canonical_as_of,
            "business_timezone": registry.business_timezone,
            "target_date": target_date,
        }
    )
    request["analysis_context"] = analysis_context
    return {
        **dict(intent),
        "time_window": target_date,
        "target_semantic": target_date,
    }


def _validated_business_intent_sequence(
    raw: Any,
    *,
    field: str,
    required: bool = False,
) -> list[Any]:
    if raw is None:
        if not required:
            return []
        raise WorkflowFailure(
            f"business_intent_contract_invalid:{field}",
            failure_type="llm_contract",
        )
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, (str, bytes, bytearray))
        or isinstance(raw, Mapping)
    ):
        raise WorkflowFailure(
            f"business_intent_contract_invalid:{field}",
            failure_type="llm_contract",
        )
    return list(raw)


def _validated_business_intent_baseline_candidates(
    raw: Any,
    *,
    production_like: bool,
) -> list[Any]:
    candidates = _validated_business_intent_sequence(
        raw,
        field="baseline_candidates",
        required=production_like,
    )
    if not production_like:
        return candidates

    canonical: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, (str, Mapping)):
            raise WorkflowFailure(
                "business_intent_contract_invalid:baseline_candidates",
                failure_type="llm_contract",
            )
        try:
            candidate_ids = canonical_baseline_ids(candidate)
        except BaselineSemanticError as exc:
            raise WorkflowFailure(
                "business_intent_contract_invalid:baseline_candidates",
                failure_type="llm_contract",
            ) from exc
        if len(candidate_ids) != 1 or candidate_ids[0] in canonical:
            raise WorkflowFailure(
                "business_intent_contract_invalid:baseline_candidates",
                failure_type="llm_contract",
            )
        canonical.append(candidate_ids[0])
    return canonical


def _material_business_intent_values(
    request: Mapping[str, Any],
    output: Mapping[str, Any],
    registry: RuntimeContractRegistry,
) -> dict[str, Any]:
    axes = (
        "question_family",
        "target_metric",
        "pattern_family",
        "scope",
        "time_window",
        "target_claim",
    )
    values = {
        axis: request[axis] if axis in request else output.get(axis)
        for axis in axes
    }
    for axis, value in values.items():
        scalar_axis = axis in {
            "question_family",
            "target_metric",
            "pattern_family",
            "target_claim",
        }
        if (
            scalar_axis
            and (not isinstance(value, str) or not value.strip())
        ) or (
            not scalar_axis
            and _empty_business_context_value(value)
        ):
            raise WorkflowFailure(
                f"business_intent_contract_invalid:{axis}",
                failure_type="llm_contract",
            )

    normalized_metric = _normalize_target_metric(values["target_metric"])
    if normalized_metric not in set(registry.metric_ids):
        raise WorkflowFailure(
            "business_intent_contract_invalid:target_metric",
            failure_type="llm_contract",
        )
    pattern_family = _normalize_pattern_family(
        values["pattern_family"],
        request,
        strict=True,
    )
    scope = _normalize_scope(values["scope"], strict=True)
    if scope not in set(registry.public_scope_types):
        raise WorkflowFailure(
            "business_intent_contract_invalid:scope",
            failure_type="llm_contract",
        )
    time_window = _validated_material_time_window(values["time_window"])
    return {
        **values,
        "question_family": str(values["question_family"]).strip(),
        "target_metric": normalized_metric,
        "pattern_family": pattern_family,
        "scope": scope,
        "time_window": time_window,
        "target_claim": _normalize_target_claim(values["target_claim"]),
    }


def _bind_clarification_attempt_intent(
    current: Mapping[str, Any],
    request: Mapping[str, Any],
    registry: RuntimeContractRegistry,
) -> dict[str, Any]:
    resume = request.get("clarification_attempt_context") or {}
    if not isinstance(resume, Mapping) or not resume:
        return dict(current)
    original = resume.get("original_intent") or {}
    if not original:
        if resume.get("material_slots"):
            raise WorkflowFailure(
                "clarification_attempt_source_material_invalid",
                failure_type="contract",
            )
        return dict(current)
    if (
        not isinstance(original, Mapping)
        or not str(resume.get("source_run_id") or "")
        or not str(resume.get("source_thread_id") or "")
        or not str(resume.get("source_topic_id") or "")
        or str(resume.get("source_thread_id"))
        != str(request.get("thread_id") or "")
        or str(resume.get("source_topic_id"))
        != str(request.get("topic_id") or "")
        or str(original.get("question") or "")
        != str(resume.get("question") or request.get("question") or "")
    ):
        raise WorkflowFailure(
            "clarification_attempt_original_intent_invalid",
            failure_type="contract",
        )
    raw_choice = request.get("clarification_choice") or {}
    if isinstance(raw_choice, Mapping):
        has_material, typed_choice = _typed_material_clarification_choice(
            {
                key: value
                for key, value in raw_choice.items()
                if key != "answer_text"
            },
            original,
        )
        if has_material and not typed_choice:
            raise WorkflowFailure(
                "clarification_attempt_material_choice_invalid",
                failure_type="contract",
            )
        if typed_choice:
            rebound_slots = {
                slot
                for field in typed_choice
                for slot in _MATERIAL_CLARIFICATION_FIELD_SLOTS.get(field, ())
            }
            current = {
                **dict(current),
                **typed_choice,
                "ambiguous_slots": [
                    item
                    for item in original.get("ambiguous_slots", ())
                    if str(
                        item.get("slot")
                        if isinstance(item, Mapping)
                        else item
                    )
                    not in rebound_slots
                ],
            }
    preserved_fields = (
        "question_family",
        "question_families",
        "primary_question_family",
        "secondary_question_families",
        "target_metric",
        "pattern_family",
        "pattern_params",
        "scope",
        "time_window",
        "target_claim",
        "baseline_candidates",
        "sub_intents",
        "ambiguous_slots",
        "answer_contract",
        "goal_bindings",
        "explicit_focus",
        "required_outcomes",
        "analysis_plan",
        "analysis_axes",
        "analysis_axis_ids",
        "context_sources",
        "required_claim_types",
        "auxiliary_claim_types",
        "publishable_claim_types",
        "dimension_ids",
        "component_ids",
        "association_metric_ids",
        "baseline",
        "target",
        "question",
    )
    original_ambiguous_slots = _ambiguous_slot_names(original)
    current_ambiguous_slots = _ambiguous_slot_names(current)
    resolved_fields = {
        field
        for field, field_slots in _MATERIAL_CLARIFICATION_FIELD_SLOTS.items()
        if field in current
        and original_ambiguous_slots.intersection(field_slots)
        and not current_ambiguous_slots.intersection(field_slots)
        and not _empty_business_context_value(current.get(field))
    }
    if "question_family" in resolved_fields:
        resolved_fields.update(
            {
                "question_families",
                "primary_question_family",
                "secondary_question_families",
            }
        )
    resolved_slots = {
        slot
        for field in resolved_fields
        for slot in _MATERIAL_CLARIFICATION_FIELD_SLOTS.get(field, ())
        if slot in original_ambiguous_slots
    }
    bound = dict(current)
    for field in preserved_fields:
        if field == "ambiguous_slots":
            continue
        if field in original and field not in resolved_fields:
            bound[field] = to_jsonable(original[field])
    bound["ambiguous_slots"] = [
        to_jsonable(item)
        for item in original.get("ambiguous_slots", ())
        if str(item.get("slot") if isinstance(item, Mapping) else item)
        not in resolved_slots
    ]
    original_requirements = {
        "goal_bindings": original.get("goal_bindings") or [],
        "explicit_focus": original.get("explicit_focus") or {},
    }
    validated_material = _validated_business_intent_requirements(
        original_requirements,
        registry,
    )
    persisted_material = _validated_resume_material_slots(
        resume.get("material_slots"), registry
    )
    _validate_source_clarification_material(original, persisted_material)
    bound.update(
        {
            field: value
            for field, value in validated_material.items()
            if field not in resolved_fields
        }
    )
    return _normalize_question_families(bound)


def _validated_resume_material_slots(
    raw: Any,
    registry: RuntimeContractRegistry,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WorkflowFailure(
            "clarification_attempt_source_material_invalid",
            failure_type="contract",
        )
    for key in MATERIAL_AUTHORITY_LIST_AXES:
        if key not in raw:
            raise WorkflowFailure(
                f"clarification_attempt_source_material_invalid:{key}",
                failure_type="contract",
            )
    allowed_values = {
        "target_metrics": set(registry.metric_ids),
        "baselines": set(CURRENT_DATA_BASELINES),
        "context_sources": set(registry.context_source_ids),
        "claim_types": {
            str(claim_type)
            for capability_id in registry.capability_ids
            for claim_type in registry.capability_inputs(capability_id).get(
                "supported_claim_types", ()
            )
        },
        "dimension_ids": set(registry.dimension_ids),
        "component_ids": set(registry.metric_ids),
        "association_metric_ids": set(registry.metric_ids),
        "analysis_axis_ids": set(registry.analysis_axis_ids),
        "required_outcomes": {
            str(outcome)
            for goal_id in registry.analysis_goal_ids
            for outcome in registry.analysis_goal_obligation(goal_id).get(
                "required_outcomes", ()
            )
        },
        "diagnostic_tags": set(registry.diagnostic_obligation_ids),
    }
    output = dict(raw)
    for key, allowed in allowed_values.items():
        if key not in raw:
            continue
        value = raw[key]
        if (
            not isinstance(value, (list, tuple))
            or any(not isinstance(item, str) or not item for item in value)
            or len(value) != len(set(value))
            or any(item not in allowed for item in value)
        ):
            raise WorkflowFailure(
                f"clarification_attempt_source_material_invalid:{key}",
                failure_type="contract",
            )
        output[key] = list(value)
    return output


def _validate_source_clarification_material(
    original: Mapping[str, Any], persisted: Mapping[str, Any]
) -> None:
    original_values = {
        "target_metrics": [str(original.get("target_metric") or "")],
        "component_ids": list(original.get("component_ids") or ()),
        "association_metric_ids": list(
            original.get("association_metric_ids") or ()
        ),
        "dimension_ids": list(original.get("dimension_ids") or ()),
        "context_sources": list(original.get("context_sources") or ()),
        "claim_types": list(original.get("publishable_claim_types") or ()),
        "required_outcomes": list(original.get("required_outcomes") or ()),
        "analysis_axis_ids": list(original.get("analysis_axis_ids") or ()),
        "baselines": _canonical_baseline_ids(
            original.get("baseline_candidates"),
            strict=True,
        ),
    }
    for axis, source_values in original_values.items():
        expected = [value for value in source_values if value]
        material_values = list(persisted.get(axis) or ())
        if set(material_values) != set(expected):
            raise WorkflowFailure(
                f"clarification_attempt_source_material_conflict:{axis}",
                failure_type="contract",
            )
    if "scope" in persisted or "scope" in original:
        if _material_scope_signature(persisted.get("scope")) != (
            _material_scope_signature(original.get("scope"))
        ):
            raise WorkflowFailure(
                "clarification_attempt_source_material_conflict:scope",
                failure_type="contract",
            )


def _ordered_resume_question_families(
    intent: Mapping[str, Any],
) -> tuple[str, ...]:
    family_fields = {
        key: intent.get(key)
        for key in (
            "question_family",
            "primary_question_family",
            "question_families",
            "secondary_question_families",
        )
        if key in intent
    }
    return tuple(_question_family_values(family_fields))


def _business_intent_payload(
    request: dict[str, Any],
    *,
    registry: RuntimeContractRegistry | None = None,
) -> dict[str, Any]:
    registry = registry or RuntimeContractRegistry.from_path(
        CANONICAL_RUNTIME_BINDINGS_PATH
    )
    baseline_semantics = baseline_llm_semantics()
    (
        context_compatibility,
        context_backed_dimensions,
        dimension_sources,
    ) = _context_family_compatibility_projection(registry)
    metric_sources = {
        metric_id: set(registry.metric_sources(metric_id))
        for metric_id in registry.metric_ids
    }
    dimension_compatibility = {
        metric_id: _target_dimension_family_compatibility(
            context_backed_dimensions,
            dimension_sources,
            target_sources,
        )
        for metric_id, target_sources in metric_sources.items()
    }
    payload: dict[str, Any] = {
        "question": request.get("question", "Explain the paid amount question."),
        "allowed_target_metric_ids": registry.metric_ids,
        "allowed_relative_target_ids": list(
            _CANONICAL_RELATIVE_TARGET_IDS
        ),
        "allowed_baseline_ids": list(CANONICAL_BASELINE_IDS),
        "allowed_baseline_semantics": list(baseline_semantics),
        "allowed_scope_types": list(registry.public_scope_types),
        "allowed_goal_ids": registry.analysis_goal_ids,
        "allowed_goal_semantics": registry.analysis_goal_semantics,
        "allowed_explicit_context_source_ids": registry.context_source_ids,
        "allowed_explicit_dimension_ids": registry.dimension_ids,
        "allowed_explicit_component_ids": registry.metric_ids,
        "explicit_focus_policy": {
            "role": "user_named_focus_only",
            "complete_analysis_scope": "local_goal_and_axis_contract",
            "empty_focus_meaning": "no_user_narrowing",
        },
        "context_source_question_family_compatibility": context_compatibility,
        "dimension_question_family_compatibility": dimension_compatibility,
    }
    raw_analysis_context = request.get("analysis_context")
    if raw_analysis_context is not None:
        if not isinstance(raw_analysis_context, Mapping):
            raise WorkflowFailure(
                "business_intent_analysis_context_invalid",
                failure_type="contract",
            )
        if "target_date" in raw_analysis_context:
            target_date = raw_analysis_context.get("target_date")
            try:
                canonical_target_date = datetime.fromisoformat(
                    target_date if isinstance(target_date, str) else ""
                ).date().isoformat()
            except ValueError:
                canonical_target_date = ""
            if canonical_target_date != target_date:
                raise WorkflowFailure(
                    "business_intent_analysis_context_invalid:target_date",
                    failure_type="contract",
                )
            payload["reviewed_time_window_recommendation"] = {
                "time_window": canonical_target_date,
                "source": "analysis_context.target_date",
            }
    context = {
        key: request[key]
        for key in (
            "target_metric",
            "pattern_family",
            "pattern_params",
            "scope",
            "time_window",
            "baseline",
            "target",
        )
        if key in request and not _empty_business_context_value(request[key])
    }
    raw_prior_context = request.get("prior_topic_material_context")
    if raw_prior_context:
        repeated_axes = tuple(
            key
            for key in _PRIOR_TOPIC_PRIVATE_MATERIAL_AXES
            if key in request
            and not _empty_business_context_value(request[key])
        )
        if repeated_axes:
            raise WorkflowFailure(
                "prior_topic_material_context_request_axis_conflict:"
                + ",".join(repeated_axes),
                failure_type="contract",
            )
        try:
            validated_prior = validate_prior_topic_material_context(
                raw_prior_context,
                thread_id=str(request.get("thread_id") or ""),
                topic_id=str(request.get("topic_id") or ""),
            )
        except EvidenceIntegrityError as exc:
            raise WorkflowFailure(
                "prior_topic_material_context_invalid:"
                f"{_exception_reason(exc)}",
                failure_type="contract",
            ) from exc
        projection = validated_prior["material_projection"]
        intent_material = projection["intent_material"]
        context.update(
            {
                "target_metric": intent_material["primary_target_metric"],
                "scope": canonical_value(intent_material["scope"]),
                "prior_baselines": list(intent_material["baselines"]),
            }
        )
        if not _empty_business_context_value(
            intent_material.get("time_window")
        ):
            context["time_window"] = canonical_value(
                intent_material["time_window"]
            )
    if context:
        payload["bound_business_context"] = context
    return payload


def _validated_business_intent_requirements(
    raw: Any,
    registry: RuntimeContractRegistry,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "goal_bindings",
        "explicit_focus",
    }:
        raise WorkflowFailure(
            "business_intent_contract_invalid:analysis_requirements",
            failure_type="llm_contract",
        )
    raw_bindings = raw.get("goal_bindings")
    if (
        not isinstance(raw_bindings, (list, tuple))
        or not raw_bindings
        or any(
            not isinstance(binding, Mapping)
            or set(binding) != {"goal_id", "role"}
            or str(binding.get("goal_id") or "")
            not in set(registry.analysis_goal_ids)
            or binding.get("role") not in {"primary", "supporting"}
            for binding in raw_bindings
        )
        or len(
            {
                str(binding["goal_id"])
                for binding in raw_bindings
            }
        )
        != len(raw_bindings)
        or sum(binding.get("role") == "primary" for binding in raw_bindings)
        != 1
    ):
        raise WorkflowFailure(
            "business_intent_contract_invalid:analysis_requirements:goal_bindings",
            failure_type="llm_contract",
        )
    raw_focus = raw.get("explicit_focus")
    focus_fields = {
        "component_ids": set(registry.metric_ids),
        "dimension_ids": set(registry.dimension_ids),
        "context_source_ids": set(registry.context_source_ids),
    }
    if not isinstance(raw_focus, Mapping) or set(raw_focus) != set(focus_fields):
        raise WorkflowFailure(
            "business_intent_contract_invalid:analysis_requirements:explicit_focus",
            failure_type="llm_contract",
        )
    normalized_focus: dict[str, list[str]] = {}
    for field, allowed in focus_fields.items():
        values = raw_focus.get(field)
        if (
            not isinstance(values, (list, tuple))
            or any(not isinstance(item, str) or not item for item in values)
            or len(values) != len(set(values))
            or any(item not in allowed for item in values)
        ):
            raise WorkflowFailure(
                "business_intent_contract_invalid:analysis_requirements:"
                f"explicit_focus:{field}",
                failure_type="llm_contract",
            )
        normalized_focus[field] = list(values)
    return {
        "goal_bindings": [
            {
                "goal_id": str(binding["goal_id"]),
                "role": str(binding["role"]),
            }
            for binding in raw_bindings
        ],
        "explicit_focus": normalized_focus,
    }


def _validate_context_family_axis(
    intent: Mapping[str, Any], registry: RuntimeContractRegistry
) -> None:
    target_metric = str(intent.get("target_metric") or "")
    (
        context_compatibility,
        context_backed_dimensions,
        dimension_sources,
    ) = _context_family_compatibility_projection(registry)
    try:
        target_sources = set(registry.metric_sources(target_metric))
    except (KeyError, TypeError, ValueError):
        target_sources = set()
    dimension_compatibility = _target_dimension_family_compatibility(
        context_backed_dimensions,
        dimension_sources,
        target_sources,
    )
    explicit_focus = intent.get("explicit_focus") or {}
    context_sources = tuple(
        str(dataset_id)
        for dataset_id in explicit_focus.get("context_source_ids") or ()
        if str(dataset_id)
    )
    selected_families = _intent_question_family_set(intent)
    for dataset_id in context_sources:
        compatible = set(context_compatibility.get(dataset_id, ()))
        if not compatible:
            raise WorkflowFailure(
                f"context_family_axis_unmapped:{dataset_id}",
                failure_type="llm_contract",
            )
        if not selected_families.intersection(compatible):
            raise WorkflowFailure(
                f"context_family_axis_missing:{dataset_id}",
                failure_type="llm_contract",
            )
    for dimension_id in explicit_focus.get("dimension_ids") or ():
        dimension_key = str(dimension_id)
        if dimension_key not in dimension_compatibility:
            continue
        compatible = set(dimension_compatibility[dimension_key])
        if not compatible:
            raise WorkflowFailure(
                f"context_family_axis_unmapped:dimension:{dimension_key}",
                failure_type="llm_contract",
            )
        if not selected_families.intersection(compatible):
            context_dataset = next(
                (
                    str(dataset_id)
                    for dataset_id in dimension_sources.get(dimension_key, ())
                    if str(dataset_id) in context_compatibility
                ),
                "unmapped",
            )
            raise WorkflowFailure(
                "context_family_axis_missing:dimension:"
                f"{dimension_key}:{context_dataset}",
                failure_type="llm_contract",
            )


def _context_family_compatibility_projection(
    registry: RuntimeContractRegistry,
) -> tuple[
    dict[str, list[str]],
    dict[str, list[str]],
    dict[str, tuple[str, ...]],
]:
    context_source_ids = tuple(str(item) for item in registry.context_source_ids)
    context_source_set = set(context_source_ids)
    context_compatibility = {
        dataset_id: sorted(
            _compatible_context_question_families(dataset_id, registry)
        )
        for dataset_id in context_source_ids
    }
    dimension_compatibility: dict[str, list[str]] = {}
    dimension_sources_by_id: dict[str, tuple[str, ...]] = {}
    for dimension_id in registry.dimension_ids:
        dimension_sources = tuple(
            str(dataset_id)
            for dataset_id in registry.dimension_sources(dimension_id)
        )
        dimension_sources_by_id[str(dimension_id)] = dimension_sources
        context_sources = tuple(
            dataset_id
            for dataset_id in dimension_sources
            if dataset_id in context_source_set
        )
        if not context_sources:
            continue
        dimension_compatibility[str(dimension_id)] = sorted(
            {
                family
                for dataset_id in context_sources
                for family in context_compatibility[dataset_id]
            }
        )
    return (
        context_compatibility,
        dimension_compatibility,
        dimension_sources_by_id,
    )


def _target_dimension_family_compatibility(
    context_backed_dimensions: Mapping[str, list[str]],
    dimension_sources: Mapping[str, Sequence[str]],
    target_sources: set[str],
) -> dict[str, list[str]]:
    return {
        dimension_id: families
        for dimension_id, families in context_backed_dimensions.items()
        if not target_sources.intersection(
            str(dataset_id)
            for dataset_id in dimension_sources.get(dimension_id, ())
        )
    }


def _compatible_context_question_families(
    dataset_id: str,
    registry: RuntimeContractRegistry,
) -> set[str]:
    return {
        family
        for family in registry.question_family_ids
        if _question_family_supports_context_dataset(
            family, dataset_id, registry
        )
    }


def _question_family_supports_context_dataset(
    question_family: str,
    dataset_id: str,
    registry: RuntimeContractRegistry,
) -> bool:
    try:
        dataset = registry.dataset(dataset_id)
    except KeyError:
        return False
    if "business_context" not in set(dataset.get("intent_roles") or ()):
        return False
    try:
        obligation = registry.question_family_obligation(question_family)
    except KeyError:
        return False
    capabilities = [
        *(obligation.get("required_capabilities") or ()),
        *(obligation.get("independent_capabilities") or ()),
        *(
            capability
            for rule in obligation.get("conditional_rules") or ()
            if isinstance(rule, Mapping)
            for capability in rule.get("add") or ()
        ),
    ]
    for capability_id in capabilities:
        try:
            contract = registry.capability_inputs(str(capability_id))
        except KeyError:
            continue
        if dataset_id in set(contract.get("allowed_datasets") or ()):
            return True
        if dataset_id in set(contract.get("allowed_context_datasets") or ()):
            return True
    return False


def _question_family_values(raw: Any) -> list[str]:
    if isinstance(raw, Mapping):
        allowed = {
            "question_family",
            "primary_question_family",
            "question_families",
            "secondary_question_families",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise WorkflowFailure(
                "question_families_mapping_contract_invalid",
                failure_type="llm_contract",
            )
        items = []
        for key in ("question_family", "primary_question_family"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                items.append(value)
            elif value not in (None, ""):
                raise WorkflowFailure(
                    "question_families_mapping_contract_invalid",
                    failure_type="llm_contract",
                )
        for key in ("question_families", "secondary_question_families"):
            value = raw.get(key) or ()
            if isinstance(value, str):
                items.append(value)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                if not all(isinstance(item, str) and item for item in value):
                    raise WorkflowFailure(
                        "question_families_mapping_contract_invalid",
                        failure_type="llm_contract",
                    )
                items.extend(value)
            else:
                raise WorkflowFailure(
                    "question_families_mapping_contract_invalid",
                    failure_type="llm_contract",
                )
        return list(dict.fromkeys(items))
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        if not all(isinstance(item, str) and item for item in raw):
            raise WorkflowFailure(
                "question_families_sequence_contract_invalid",
                failure_type="llm_contract",
            )
        return list(dict.fromkeys(raw))
    if isinstance(raw, str):
        return [raw] if raw else []
    if raw:
        raise WorkflowFailure(
            "question_families_contract_invalid",
            failure_type="llm_contract",
        )
    return []


def _normalize_question_families(
    intent: dict[str, Any],
    *,
    registry: RuntimeContractRegistry | None = None,
) -> dict[str, Any]:
    registry = registry or RuntimeContractRegistry.from_path(
        CANONICAL_RUNTIME_BINDINGS_PATH
    )
    canonical_ids = set(registry.question_family_ids)
    diagnostic_ids = set(registry.diagnostic_obligation_ids)
    explicit_primary = str(intent.get("primary_question_family") or "")
    question_family = str(intent.get("question_family") or "")
    primary_candidates = tuple(
        family for family in (explicit_primary, question_family) if family
    )
    family_values = tuple(
        dict.fromkeys(
            (
                *primary_candidates,
                *_question_family_values(intent.get("question_families", ())),
                *_question_family_values(
                    intent.get("secondary_question_families", ())
                ),
            )
        )
    )
    unknown = tuple(
        family
        for family in family_values
        if family not in canonical_ids and family not in diagnostic_ids
    )
    if unknown:
        raise WorkflowFailure(
            f"unknown_question_family_or_diagnostic:{unknown[0]}",
            failure_type="llm_contract",
        )
    canonical_primary_candidates = set(primary_candidates) & canonical_ids
    if len(canonical_primary_candidates) > 1:
        raise WorkflowFailure(
            "question_family_primary_conflict:"
            f"{','.join(sorted(canonical_primary_candidates))}",
            failure_type="llm_contract",
        )
    diagnostic_candidates = tuple(
        family for family in family_values if family in diagnostic_ids
    )
    diagnostic_support = {
        family: set(
            registry.diagnostic_obligation(family)["supported_question_families"]
        )
        for family in diagnostic_candidates
    }
    if canonical_primary_candidates:
        primary = next(iter(canonical_primary_candidates))
        for family, supported in diagnostic_support.items():
            if primary not in supported:
                raise WorkflowFailure(
                    "diagnostic_question_family_incompatible:"
                    f"{family}:{primary}",
                    failure_type="llm_contract",
                )
    elif diagnostic_support:
        supported_intersection = set.intersection(*diagnostic_support.values())
        if len(supported_intersection) == 1:
            primary = next(iter(supported_intersection))
        elif not supported_intersection:
            supported_union = set().union(*diagnostic_support.values())
            raise WorkflowFailure(
                "question_family_primary_conflict:"
                f"{','.join(sorted(supported_union))}",
                failure_type="llm_contract",
            )
        elif len(diagnostic_support) == 1:
            raise WorkflowFailure(
                "diagnostic_question_family_ambiguous:"
                f"{next(iter(diagnostic_support))}",
                failure_type="llm_contract",
            )
        else:
            raise WorkflowFailure(
                "question_family_primary_ambiguous:"
                f"{','.join(sorted(supported_intersection))}",
                failure_type="llm_contract",
            )
    else:
        primary = "pattern_explanation"

    def canonical_family(value: Any) -> str:
        family = str(value or "")
        if family in canonical_ids:
            return family
        return primary
    families = list(
        dict.fromkeys(
            canonical_family(item)
            for item in _question_family_values(intent.get("question_families", ()))
        )
    )
    if primary not in families:
        families.insert(0, primary)
    secondary = list(
        dict.fromkeys(
            canonical_family(item)
            for item in _question_family_values(
                intent.get("secondary_question_families", ())
            )
            if canonical_family(item) != primary
        )
    )
    for family in families:
        if family != primary and family not in secondary:
            secondary.append(family)
    families = [primary, *(family for family in secondary if family != primary)]
    return {
        **intent,
        "question_family": primary,
        "primary_question_family": primary,
        "question_families": families,
        "secondary_question_families": secondary,
    }


def _normalize_scope(scope: Any, *, strict: bool = False) -> str:
    if isinstance(scope, Mapping):
        meaningful = {
            str(key): value
            for key, value in scope.items()
            if value not in (None, "", {}, [])
        }
        token_keys = {"value", "type", "label", "scope"}
        if strict and (not meaningful or set(meaningful) - token_keys):
            raise WorkflowFailure(
                "business_intent_contract_invalid:scope",
                failure_type="llm_contract",
            )
        tokens = [
            str(meaningful[key]).strip()
            for key in ("value", "type", "label", "scope")
            if key in meaningful
        ]
        if strict and (
            not tokens
            or any(not token for token in tokens)
            or len({_normalize_scope(token) for token in tokens}) != 1
        ):
            raise WorkflowFailure(
                "business_intent_contract_invalid:scope",
                failure_type="llm_contract",
            )
        scope = tokens[0] if tokens else None
    if strict and (not isinstance(scope, str) or not scope.strip()):
        raise WorkflowFailure(
            "business_intent_contract_invalid:scope",
            failure_type="llm_contract",
        )
    value = str(scope or "").strip()
    aliases = {
        "all",
        "all users",
        "all_users",
        "all_sample",
        "entire_sample",
        "full",
        "full sample",
        "full_sample",
        "overall",
        "全部",
        "全量",
        "全量样本",
        "全样本",
        "整体",
        "整体样本",
        "全体",
        "全体用户",
        "全部用户",
        "所有用户",
    }
    if value.lower() in aliases:
        return "full_sample"
    return value or "full_sample"


def _validated_material_time_window(time_window: Any) -> Any:
    """Accept canonical target windows without inventing time semantics."""

    def canonical_date(value: str) -> bool:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return False
        try:
            return datetime.fromisoformat(value).date().isoformat() == value
        except ValueError:
            return False

    def canonical_month(value: str) -> bool:
        match = re.fullmatch(r"(\d{4})-(\d{2})", value)
        if match is None:
            return False
        return 1 <= int(match.group(2)) <= 12

    def canonical_scalar(value: str) -> bool:
        token = value.strip()
        if token in _CANONICAL_RELATIVE_TARGET_ID_SET:
            return True
        if canonical_date(token):
            return True
        if ".." not in token:
            return False
        parts = token.split("..")
        if len(parts) != 2:
            return False
        start, end = parts
        if canonical_date(start) and canonical_date(end):
            return start <= end
        if canonical_month(start) and canonical_month(end):
            return start <= end
        return False

    def valid(value: Any, *, field: str = "") -> bool:
        if isinstance(value, str):
            if field in _STRUCTURED_DATE_BOUND_KEYS:
                return canonical_date(value.strip())
            if not field or field in _STRUCTURED_TARGET_TIME_KEYS:
                return canonical_scalar(value)
            return bool(value.strip())
        if isinstance(value, bool) or value is None:
            return False
        if isinstance(value, (int, float)):
            return True
        if isinstance(value, Mapping):
            return bool(value) and all(
                isinstance(key, str)
                and key.strip()
                and valid(item, field=key)
                for key, item in value.items()
            )
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return bool(value) and all(valid(item, field=field) for item in value)
        return False

    if not valid(time_window):
        raise WorkflowFailure(
            "business_intent_contract_invalid:time_window",
            failure_type="llm_contract",
        )
    return time_window


def _material_scope_signature(scope: Any) -> tuple[str, str] | None:
    if scope in (None, "", {}, []):
        return None
    if not isinstance(scope, Mapping):
        return ("token", _normalize_scope(scope))
    meaningful = {
        str(key): value
        for key, value in scope.items()
        if value not in (None, "", {}, [])
    }
    if not meaningful:
        return None
    token_keys = {"value", "type", "label", "scope"}
    token_values = [
        _normalize_scope(value)
        for key, value in meaningful.items()
        if key in token_keys
    ]
    extras = set(meaningful) - token_keys
    if token_values and not extras and len(set(token_values)) == 1:
        return ("token", token_values[0])
    return (
        "mapping",
        json.dumps(
            to_jsonable(meaningful),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _normalize_target_claim(value: Any) -> str:
    text = str(value or "pattern_explanation")
    return text.replace("统计显著", "符合重要性规则").replace("显著", "明显")


def _normalize_target_metric(metric: Any) -> str:
    value = str(metric or "").strip()
    aliases = {
        "paid_amount",
        "payment_amount",
        "paid_amount_ngn",
        "daily_paid_amount",
        "avg_paid_amount",
        "avg_daily_paid_amount",
        "daily_average_paid_amount",
        "monthly_daily_avg_paid_amount",
        "monthly_avg_paid_amount",
    }
    if value in aliases:
        return "paid_amount"
    return value or "paid_amount"


def _validate_business_intent_pattern_output(output: Mapping[str, Any]) -> None:
    pattern_family = output.get("pattern_family")
    if (
        not isinstance(pattern_family, str)
        or pattern_family not in _SUPPORTED_PATTERN_FAMILIES
    ):
        raise LLMOutputError("invalid_llm_output_material:pattern_family")
    pattern_params = output.get("pattern_params")
    if not isinstance(pattern_params, Mapping):
        raise LLMOutputError("invalid_llm_output_material:pattern_params")
    if pattern_family == "weekly" and not _weekly_pattern_has_weekday_target(
        pattern_params
    ):
        raise LLMOutputError(
            "invalid_llm_output_material:pattern_params:weekly_target_required"
        )
    if pattern_family == "intra_period" and not _intra_period_has_target(
        pattern_params
    ):
        raise LLMOutputError(
            "invalid_llm_output_material:pattern_params:intra_period_target_required"
        )


def _validate_business_intent_provider_output(
    output: Mapping[str, Any],
    request: Mapping[str, Any],
    registry: RuntimeContractRegistry,
) -> None:
    _validate_business_intent_pattern_output(output)
    try:
        material = _material_business_intent_values(request, output, registry)
        requirements = _validated_business_intent_requirements(
            output.get("analysis_requirements"),
            registry,
        )
        intent = _normalize_question_families(
            {
                "question_family": material["question_family"],
                "primary_question_family": output.get(
                    "primary_question_family"
                ),
                "question_families": output.get("question_families") or (),
                "secondary_question_families": output.get(
                    "secondary_question_families"
                )
                or (),
                "target_metric": material["target_metric"],
                **requirements,
            },
            registry=registry,
        )
        _validate_context_family_axis(intent, registry)
    except WorkflowFailure as exc:
        raise LLMOutputError(_exception_reason(exc)) from exc


def _normalize_pattern_family(
    pattern_family: Any,
    request: Mapping[str, Any],
    *,
    strict: bool = False,
) -> str:
    request_value = str(request.get("pattern_family") or "").strip().lower()
    if request_value in _SUPPORTED_PATTERN_FAMILIES:
        return request_value
    value = str(pattern_family or "").strip().lower()
    if value in _SUPPORTED_PATTERN_FAMILIES:
        return value
    if strict:
        raise WorkflowFailure(
            "business_intent_contract_invalid:pattern_family",
            failure_type="llm_contract",
        )
    return "intra_period"


def _normalize_pattern_params(
    request: Mapping[str, Any],
    output: Mapping[str, Any],
    pattern_family: str,
    *,
    allow_question_inference: bool = True,
    require_output_mapping: bool = False,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    output_params = output.get("pattern_params")
    if isinstance(output_params, Mapping):
        params.update(dict(output_params))
    elif require_output_mapping or output_params not in (None, "", {}, []):
        raise WorkflowFailure(
            "business_intent_contract_invalid:pattern_params",
            failure_type="llm_contract",
        )
    request_params = request.get("pattern_params")
    if isinstance(request_params, Mapping):
        params.update(dict(request_params))
    elif "pattern_params" in request and request_params not in (None, "", {}, []):
        raise WorkflowFailure(
            "business_intent_contract_invalid:pattern_params",
            failure_type="llm_contract",
        )

    if allow_question_inference:
        question = str(request.get("question") or "")
        if pattern_family == "weekly" and "周末" in question:
            params.setdefault("weekday_key", "weekday")
            params.setdefault("target_weekdays", [6, 7])
            params.setdefault("baseline_weekdays", [1, 2, 3, 4, 5])
        if pattern_family == "intra_period" and "月初" in question:
            params.setdefault("target_phase", "start")
    return params


def _repair_pattern_family_and_params(
    pattern_family: str,
    pattern_params: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    params = dict(pattern_params)
    if pattern_family == "weekly" and not _weekly_pattern_has_weekday_target(params):
        return "rolling", params
    return pattern_family, params


def _weekly_pattern_has_weekday_target(pattern_params: Mapping[str, Any]) -> bool:
    return _valid_pattern_selector_scalar(
        pattern_params.get("target_weekday")
    ) or _valid_pattern_selector_sequence(pattern_params.get("target_weekdays"))


def _intra_period_has_target(pattern_params: Mapping[str, Any]) -> bool:
    return _valid_pattern_selector_scalar(
        pattern_params.get("target_phase")
    ) or _valid_pattern_selector_scalar(pattern_params.get("target_group"))


def _valid_pattern_selector_scalar(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, (int, float))


def _valid_pattern_selector_sequence(value: Any) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and bool(value)
        and all(_valid_pattern_selector_scalar(item) for item in value)
    )


def _empty_business_context_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value:
        return True
    if isinstance(value, (dict, list, tuple, set)) and not value:
        return True
    return False


def _decide_question_boundary(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "decide_question_boundary")
    boundary_payload = {
        "intent": state["intent"],
        "bound_business_context": {
            "scope": state["intent"]["scope"],
            "time_window": state["intent"]["time_window"],
            "pattern_family": state["intent"]["pattern_family"],
        },
        "allowed_baseline_ids": list(CANONICAL_BASELINE_IDS),
        "allowed_baseline_semantics": list(baseline_llm_semantics()),
        "phase4_policy": "ask only when ambiguity can change conclusion, baseline, time semantics, analysis scope, claim strength, or cost",
    }
    output = _invoke_llm(
        state,
        "boundary_decision",
        boundary_payload,
        output_validator=lambda candidate: (
            _validate_boundary_decision_provider_output_for_state(
                state,
                candidate,
            )
        ),
    )
    decision = _normalize_boundary_decision_output(output)
    state["boundary_decision"] = _enforce_material_clarification_boundary(
        state,
        decision,
    )
    return state


def _enforce_material_clarification_boundary(
    state: WorkflowState,
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    """Open a business clarification when a comparison baseline is unbound."""

    if decision.get("boundary_status") == "cannot_answer":
        return dict(decision)
    if not _material_baseline_clarification_needed(state):
        return dict(decision)
    recommended = "跟前一天比较（推荐）"
    return {
        **dict(decision),
        "boundary_status": "needs_question",
        "clarification_questions": [
            {
                "question": "你希望把目标日期的付费金额与哪个基准比较？",
                "options": [
                    recommended,
                    "跟近7日均值比较",
                    "跟上周同日比较",
                    CLARIFICATION_ESCAPE_OPTION,
                ],
            }
        ],
        "recommended_assumption": {"option": recommended},
        "decision_summary": (
            "比较基准会改变变化方向和因素贡献，需要先由用户确认。"
        ),
        "display_summary": (
            "比较基准尚未确定，需要用户选择后再验证变化方向。"
        ),
        "provider_boundary_status": str(
            decision.get("boundary_status") or ""
        ),
        "local_policy_override": "missing_material_comparison_baseline",
    }


def _validate_boundary_decision_provider_output(
    output: Mapping[str, Any],
) -> None:
    try:
        _normalize_boundary_decision_output(output)
    except WorkflowFailure as exc:
        raise LLMOutputError(_exception_reason(exc)) from exc


def _validate_boundary_decision_provider_output_for_state(
    state: WorkflowState,
    output: Mapping[str, Any],
) -> None:
    _validate_boundary_decision_provider_output(output)
    status = str(output.get("boundary_status") or "")
    if (
        status not in {"needs_question", "cannot_answer"}
        and _material_baseline_clarification_needed(state)
    ):
        raise LLMOutputError(
            "boundary_decision_semantic_invalid:baseline_unbound"
        )


def _normalize_boundary_decision_output(output: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(output, Mapping):
        raise WorkflowFailure(
            "boundary_decision_contract_invalid:output",
            failure_type="llm_contract",
        )
    status = output.get("boundary_status")
    if status not in {
        "clear",
        "low_risk_assumption",
        "needs_question",
        "cannot_answer",
    }:
        raise WorkflowFailure(
            "boundary_decision_contract_invalid:boundary_status",
            failure_type="llm_contract",
        )
    questions = output.get("clarification_questions")
    if not isinstance(questions, list):
        raise WorkflowFailure(
            "boundary_decision_contract_invalid:questions",
            failure_type="llm_contract",
        )
    if status != "needs_question":
        if questions:
            raise WorkflowFailure(
                "boundary_decision_contract_invalid:questions",
                failure_type="llm_contract",
            )
        recommended = output.get("recommended_assumption")
        if status in {"clear", "cannot_answer"}:
            if not isinstance(recommended, Mapping) or recommended:
                raise WorkflowFailure(
                    "boundary_decision_contract_invalid:recommended_assumption",
                    failure_type="llm_contract",
                )
            normalized_recommended: dict[str, str] = {}
        else:
            option = recommended.get("option") if isinstance(recommended, Mapping) else None
            if (
                not isinstance(recommended, Mapping)
                or set(recommended) != {"option"}
                or not isinstance(option, str)
                or not option
                or option != option.strip()
                or not re.search(r"[\u4e00-\u9fff]", option)
                or _has_internal_visible_token(option)
            ):
                raise WorkflowFailure(
                    "boundary_decision_contract_invalid:recommended_assumption",
                    failure_type="llm_contract",
                )
            normalized_recommended = {"option": option}
        return {
            **dict(output),
            "clarification_questions": [],
            "recommended_assumption": normalized_recommended,
        }
    if len(questions) != 1 or not isinstance(questions[0], Mapping):
        raise WorkflowFailure(
            "boundary_decision_contract_invalid:questions",
            failure_type="llm_contract",
        )
    question = questions[0]
    if set(question) != {"question", "options"}:
        raise WorkflowFailure(
            "boundary_decision_contract_invalid:questions",
            failure_type="llm_contract",
        )
    question_text = question.get("question")
    raw_options = question.get("options")
    if (
        not isinstance(question_text, str)
        or not question_text
        or question_text != question_text.strip()
        or not isinstance(raw_options, list)
        or len(raw_options) not in {3, 4}
        or any(not isinstance(option, str) for option in raw_options)
        or raw_options[-1] != CLARIFICATION_ESCAPE_OPTION
        or any(not option or option != option.strip() for option in raw_options[:-1])
    ):
        raise WorkflowFailure(
            "boundary_decision_contract_invalid:options",
            failure_type="llm_contract",
        )
    recommended = output.get("recommended_assumption")
    if (
        not isinstance(recommended, Mapping)
        or set(recommended) != {"option"}
        or not isinstance(recommended.get("option"), str)
        or recommended.get("option") not in raw_options[:-1]
    ):
        raise WorkflowFailure(
            "boundary_decision_contract_invalid:recommended_option",
            failure_type="llm_contract",
        )
    try:
        normalized = _normalize_general_clarification_output({
            "questions": questions,
            "recommended_assumption": recommended,
        })
    except WorkflowFailure as exc:
        reason = str(exc)
        prefix = "general_clarification_contract_invalid:"
        suffix = reason[len(prefix):] if reason.startswith(prefix) else "questions"
        raise WorkflowFailure(
            f"boundary_decision_contract_invalid:{suffix}",
            failure_type="llm_contract",
        ) from exc
    return {
        **dict(output),
        "clarification_questions": normalized["questions"],
        "recommended_assumption": normalized["recommended_assumption"],
    }


def _local_question_boundary_decision(state: WorkflowState) -> dict[str, Any]:
    intent = state.get("intent", {})
    text = _intent_business_text(intent)
    if (
        _business_text_requests_outlier_recalc(text)
        and not state.get("request", {}).get("clarification_choice")
    ):
        return {
            "boundary_status": "needs_question",
            "recommended_assumption": (
                "按日粒度移除贡献最大的正向日期后复算，不做订单级明细剔除。"
            ),
            "clarification_questions": _local_outlier_clarification_questions(),
            "decision_summary": "异常日期剔除方式会改变业务结论，需要用户确认执行边界。",
        }
    return {
        "boundary_status": "clear",
        "recommended_assumption": {},
        "clarification_questions": [],
        "decision_summary": "当前业务边界足够明确，可以继续。",
    }


def _clarification_policy_gate(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "clarification_policy_gate")
    decision = state["boundary_decision"]
    status = decision.get("boundary_status", "clear")
    if status not in {"clear", "low_risk_assumption", "needs_question", "cannot_answer"}:
        status = "needs_question"
    raw_choice = (
        None
        if state.get("clarification_choice_consumed")
        else state["request"].get("clarification_choice")
    )
    display_answer_text = ""
    if isinstance(raw_choice, Mapping) and raw_choice:
        raw_answer_text = raw_choice.get("answer_text")
        if isinstance(raw_answer_text, str):
            display_answer_text = raw_answer_text.strip()
        authority_candidate = {
            key: value
            for key, value in raw_choice.items()
            if key != "answer_text"
        }
        has_material, typed_choice = _typed_material_clarification_choice(
            authority_candidate,
            state.get("intent") or {},
        )
        if has_material:
            if typed_choice:
                status = "low_risk_assumption"
                choice = typed_choice
                requires_rebind = True
            else:
                status = "needs_question"
                choice = {}
                requires_rebind = False
            state["clarification_outcome"] = {
                "status": "user_selected" if choice else "pending",
                "boundary_status": status,
                "recommended_assumption": (
                    "已按用户澄清继续执行，并把该选择写入本次分析边界。"
                    if choice
                    else decision.get("recommended_assumption")
                ),
                "choice": choice,
                "requires_rebind": requires_rebind,
                "display_answer_text": display_answer_text,
            }
            _current_event(state)["route"] = status
            return state
    if (
        status == "needs_question"
        and state.get("clarification_choice_consumed")
        and _boundary_repeats_bound_material_choice(state, decision)
    ):
        status = "low_risk_assumption"
        decision = {
            **decision,
            "recommended_assumption": (
                "沿用用户刚刚确认的业务口径继续。"
            ),
        }
    if status == "needs_question" and _has_bound_intra_period_comparison(state):
        status = "low_risk_assumption"
        decision = {
            **decision,
            "recommended_assumption": (
                "沿用问题中已经绑定的月初、月中和月末窗口规则继续评估。"
            ),
        }
    if status == "needs_question" and _can_continue_with_default_business_boundary(
        state, decision
    ):
        status = "low_risk_assumption"
        decision = {
            **decision,
            "recommended_assumption": decision.get("recommended_assumption")
            or "采用产品默认业务假设继续，并把假设写入本次分析边界。",
        }
    if status == "needs_question" and _can_continue_with_observational_attribution_boundary(state):
        status = "low_risk_assumption"
        decision = {
            **decision,
            "recommended_assumption": (
                "按观察性归因继续分析，只发布贡献强弱和候选解释，"
                "不把单个渠道或分群写成已证明原因。"
            ),
        }
    if status == "needs_question" and _can_continue_with_current_topic_segment_boundary(state):
        status = "low_risk_assumption"
        decision = {
            **decision,
            "recommended_assumption": (
                "沿用当前 topic 的时间窗口和基线口径继续，"
                "按渠道贡献变化做有边界的对比。"
            ),
        }
    grain_assumption = _current_topic_grain_assumption(state)
    if status == "needs_question" and grain_assumption:
        status = "low_risk_assumption"
        decision = {
            **decision,
            "recommended_assumption": grain_assumption,
        }
    actionability_assumption = _current_topic_actionability_assumption(state)
    if status == "needs_question" and actionability_assumption:
        status = "low_risk_assumption"
        decision = {
            **decision,
            "recommended_assumption": actionability_assumption,
        }
    if status == "needs_question" and not state["request"].get("allow_question_interrupt", True):
        status = "low_risk_assumption"
    state["clarification_outcome"] = {
        "status": "pending" if status == "needs_question" else "system_inferred",
        "boundary_status": status,
        "recommended_assumption": decision.get("recommended_assumption"),
        "choice": {},
        "display_answer_text": display_answer_text,
    }
    _current_event(state)["route"] = status
    return state


_MATERIAL_CLARIFICATION_CHOICE_FIELDS = frozenset(
    {
        "question_family",
        "target_metric",
        "pattern_family",
        "pattern_params",
        "scope",
        "time_window",
        "target_claim",
        "baseline_candidates",
        "goal_bindings",
        "explicit_focus",
    }
)

_MATERIAL_CLARIFICATION_FIELD_SLOTS = {
    "question_family": {"question_family", "question_families"},
    "target_metric": {"target_metric", "metric"},
    "pattern_family": {"pattern_family", "pattern"},
    "pattern_params": {"pattern_family", "pattern", "pattern_params"},
    "scope": {"scope"},
    "time_window": {"time_window", "date_range"},
    "target_claim": {"target_claim", "claim_strength"},
    "baseline_candidates": {"baseline", "baselines"},
    "goal_bindings": {"goal", "business_goal"},
    "explicit_focus": {"component", "dimension", "context_sources", "source"},
}


def _typed_material_clarification_choice(
    choice: Mapping[str, Any],
    intent: Mapping[str, Any],
) -> tuple[bool, dict[str, Any]]:
    if set(choice) - _MATERIAL_CLARIFICATION_CHOICE_FIELDS:
        return False, {}
    material_fields = tuple(choice)
    if not material_fields:
        return False, {}
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    ambiguous = _ambiguous_slot_names(intent)
    allowed_sequences = {
        "baseline_candidates": set(CURRENT_DATA_BASELINES),
    }
    allowed_scalars = {
        "question_family": set(registry.question_family_ids),
        "primary_question_family": set(registry.question_family_ids),
        "target_metric": set(registry.metric_ids),
        "pattern_family": set(_SUPPORTED_PATTERN_FAMILIES),
        "scope": set(registry.public_scope_types),
    }
    projection: dict[str, Any] = {}
    for field in material_fields:
        value = choice[field]
        if field in allowed_sequences:
            if (
                not isinstance(value, (list, tuple))
                or any(not isinstance(item, str) or not item for item in value)
                or len(value) != len(set(value))
                or any(item not in allowed_sequences[field] for item in value)
            ):
                return True, {}
            normalized: Any = list(value)
        elif field in allowed_scalars:
            if not isinstance(value, str) or value not in allowed_scalars[field]:
                return True, {}
            normalized = value
        elif field in {"goal_bindings", "explicit_focus"}:
            try:
                normalized_requirements = _validated_business_intent_requirements(
                    {
                        "goal_bindings": (
                            value
                            if field == "goal_bindings"
                            else intent.get("goal_bindings") or ()
                        ),
                        "explicit_focus": (
                            value
                            if field == "explicit_focus"
                            else intent.get("explicit_focus") or {}
                        ),
                    },
                    registry,
                )
            except WorkflowFailure:
                return True, {}
            normalized = normalized_requirements[field]
        elif field == "pattern_params":
            if not isinstance(value, Mapping):
                return True, {}
            normalized = to_jsonable(value)
        elif not isinstance(value, str) or not value.strip():
            return True, {}
        else:
            normalized = value
        if (
            canonical_value(normalized) != canonical_value(intent.get(field))
            and not ambiguous.intersection(
                _MATERIAL_CLARIFICATION_FIELD_SLOTS[field]
            )
        ):
            return True, {}
        projection[field] = normalized
    candidate = {**dict(intent), **projection}
    if "question_family" in projection:
        family = projection["question_family"]
        candidate.update(
            {
                "question_family": family,
                "primary_question_family": family,
                "question_families": [family],
                "secondary_question_families": [],
            }
        )
    try:
        _validate_business_intent_pattern_output(candidate)
        normalized_candidate = _normalize_question_families(
            candidate,
            registry=registry,
        )
        _validate_context_family_axis(normalized_candidate, registry)
    except (LLMOutputError, WorkflowFailure):
        return True, {}
    return True, projection


def _boundary_repeats_bound_material_choice(
    state: WorkflowState,
    decision: Mapping[str, Any],
) -> bool:
    raw_choice = state.get("request", {}).get("clarification_choice") or {}
    if not isinstance(raw_choice, Mapping):
        return False
    material_choice = {
        key: value
        for key, value in raw_choice.items()
        if key in _MATERIAL_CLARIFICATION_CHOICE_FIELDS
    }
    if set(material_choice) != {"baseline_candidates"}:
        return False
    intent = state.get("intent") or {}
    if canonical_value(material_choice["baseline_candidates"]) != canonical_value(
        intent.get("baseline_candidates")
    ):
        return False
    if _ambiguous_slot_names(intent).intersection({"baseline", "baselines"}):
        return False
    decision_text = json.dumps(
        to_jsonable(decision),
        ensure_ascii=False,
        sort_keys=True,
    )
    return any(
        token in decision_text
        for token in ("基线", "基准", "前一天", "上周同日", "近7日")
    )


def _can_continue_with_default_business_boundary(
    state: WorkflowState, decision: Mapping[str, Any]
) -> bool:
    intent = state.get("intent", {})
    if "revenue_health_review" not in _intent_question_family_set(intent):
        return False
    if not decision.get("recommended_assumption"):
        return False
    hard_slots = {"target_metric", "metric", "time_window", "date_range", "scope"}
    ambiguous = {
        str(item.get("slot") if isinstance(item, Mapping) else item)
        for item in intent.get("ambiguous_slots", ())
        if item
    }
    return not bool(ambiguous & hard_slots)


def _can_continue_with_observational_attribution_boundary(state: WorkflowState) -> bool:
    intent = state.get("intent", {})
    if "segment_or_factor_attribution" not in _intent_question_family_set(intent):
        return False
    text = _intent_business_text(intent)
    return _contains_segment_dimension(text) and _contains_any(
        text,
        ("主要原因", "原因", "解释"),
    )


def _can_continue_with_current_topic_segment_boundary(state: WorkflowState) -> bool:
    if not _request_has_topic_context(state):
        return False
    intent = state.get("intent", {})
    if "segment_or_factor_attribution" not in _intent_question_family_set(intent):
        return False
    text = _intent_business_text(intent)
    ambiguous = _ambiguous_slot_names(intent)
    return (
        _contains_segment_dimension(text)
        and _contains_any(text, ("变化", "贡献", "最明显", "最大"))
        and ambiguous.issubset({"baseline", "change_measure", "comparison_period", "segment_grain"})
    )


def _current_topic_grain_assumption(state: WorkflowState) -> str:
    if not _request_has_topic_context(state):
        return ""
    text = _intent_business_text(state.get("intent", {}))
    if _contains_any(text, ("日均", "日平均")):
        return "沿用当前 topic 的指标、范围和基线，按日均口径重新比较并保留口径变化说明。"
    if _contains_any(text, ("按周", "周粒度", "按周看", "口径改成按周")):
        return "沿用当前 topic 的指标、范围和基线，按周粒度重新比较并保留口径变化说明。"
    return ""


def _current_topic_actionability_assumption(state: WorkflowState) -> str:
    if not _request_has_topic_context(state):
        return ""
    text = _intent_business_text(state.get("intent", {}))
    if not _business_text_requests_actionability_verification(text):
        return ""
    return (
        "沿用当前 topic 的指标、范围和证据边界继续验证；"
        "当前结果只能作为投放排查线索，不能直接写成已证明可指导投放。"
    )


def _request_has_topic_context(state: WorkflowState) -> bool:
    manifest = state.get("request", {}).get("context_manifest") or {}
    for item in manifest.get("items", ()) or ():
        if isinstance(item, Mapping) and item.get("source_type") == "topic":
            return True
    return False


def _ambiguous_slot_names(intent: Mapping[str, Any]) -> set[str]:
    return {
        str(item.get("slot") if isinstance(item, Mapping) else item)
        for item in intent.get("ambiguous_slots", ())
        if item
    }


def _generate_clarification(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "generate_clarification")
    prior_outcome = state.get("clarification_outcome") or {}
    choice = prior_outcome.get("choice")
    display_answer_text = str(
        prior_outcome.get("display_answer_text") or ""
    ).strip()
    clarification_payload = {
        "intent": state.get("intent", {}),
        "boundary_decision": state.get("boundary_decision", {}),
        "clarification_choice": choice,
    }
    raw_output = _invoke_llm(state, "clarification_question", clarification_payload)
    output = _normalize_general_clarification_output(raw_output)
    output = _review_material_clarification_output(state, output)
    state["clarification_outcome"] = {
        "status": "user_selected" if choice else "question_tool_opened",
        "boundary_status": "needs_question"
        if state.get("next_action", {}).get("next_action") == "ask_question"
        else state["boundary_decision"].get("boundary_status"),
        "questions": output.get("questions")
        or state["boundary_decision"].get("clarification_questions", []),
        "recommended_assumption": output.get("recommended_assumption")
        or state["boundary_decision"].get("recommended_assumption"),
        "choice": choice,
        "display_answer_text": display_answer_text,
        "choice_actions": list(output.get("choice_actions") or ()),
        "recommended_choice_id": str(
            output.get("recommended_choice_id") or ""
        ),
    }
    return state


def _normalize_general_clarification_output(
    output: Mapping[str, Any],
) -> dict[str, Any]:
    def option_text(raw: Any) -> str:
        if isinstance(raw, str):
            return raw.strip()
        if not isinstance(raw, Mapping):
            return ""
        unknown = set(raw) - {"label", "description"}
        label = raw.get("label")
        description = raw.get("description")
        if (
            unknown
            or not isinstance(label, str)
            or not label.strip()
            or description is not None
            and not isinstance(description, str)
            or _has_internal_visible_token(str(description or ""))
        ):
            raise WorkflowFailure(
                "general_clarification_contract_invalid:option_object",
                failure_type="llm_contract",
            )
        return label.strip()

    questions = output.get("questions")
    if isinstance(questions, Mapping):
        questions = [questions]
    if not questions and (output.get("question") or output.get("question_text")):
        questions = [{
            "question": output.get("question") or output.get("question_text"),
            "options": output.get("options") or output.get("choices"),
        }]
    if (
        not isinstance(questions, Sequence)
        or isinstance(questions, (str, bytes))
        or len(questions) != 1
        or not isinstance(questions[0], Mapping)
    ):
        raise WorkflowFailure(
            "general_clarification_contract_invalid:question_count",
            failure_type="llm_contract",
        )
    question = str(
        questions[0].get("question")
        or questions[0].get("question_text")
        or questions[0].get("prompt")
        or questions[0].get("text")
        or ""
    ).strip()
    raw_options = questions[0].get("options") or questions[0].get("choices")
    if (
        not question
        or not isinstance(raw_options, Sequence)
        or isinstance(raw_options, (str, bytes))
    ):
        raise WorkflowFailure(
            "general_clarification_contract_invalid:question_shape",
            failure_type="llm_contract",
        )
    if not raw_options or raw_options[-1] != CLARIFICATION_ESCAPE_OPTION:
        raise WorkflowFailure(
            "general_clarification_contract_invalid:options",
            failure_type="llm_contract",
        )
    options = [value for item in raw_options if (value := option_text(item))]
    business_options = options[:-1]
    if (
        not 2 <= len(business_options) <= 3
        or len(options) != len(business_options) + 1
        or len(set(options)) != len(options)
        or options[-1] != CLARIFICATION_ESCAPE_OPTION
        or CLARIFICATION_ESCAPE_OPTION in business_options
        or any(_has_internal_visible_token(item) for item in business_options)
    ):
        raise WorkflowFailure(
            "general_clarification_contract_invalid:options",
            failure_type="llm_contract",
        )
    raw_recommended = output.get("recommended_assumption")
    if isinstance(raw_recommended, Mapping):
        recommended = str(
            raw_recommended.get("option")
            or raw_recommended.get("label")
            or ""
        ).strip()
    else:
        recommended = str(raw_recommended or "").strip()
    if recommended not in business_options:
        raise WorkflowFailure(
            "general_clarification_contract_invalid:recommended_option",
            failure_type="llm_contract",
        )
    return {
        **dict(output),
        "questions": [{"question": question, "options": options}],
        "recommended_assumption": {"option": recommended},
    }


_BASELINE_CLARIFICATION_OPTIONS = (
    (
        "previous_day",
        "跟前一天比较（推荐）",
        ("前一天", "前一日", "前日"),
    ),
    (
        "rolling_7_day_baseline",
        "跟近7日均值比较",
        ("近7日", "近7天", "7日均值", "7天均值"),
    ),
    (
        "same_weekday_last_week",
        "跟上周同日比较",
        ("上周同日",),
    ),
)


def _review_material_clarification_output(
    state: WorkflowState,
    output: Mapping[str, Any],
) -> dict[str, Any]:
    if not _baseline_clarification_required(state):
        return dict(output)
    questions = list(output.get("questions") or ())
    if len(questions) != 1 or not isinstance(questions[0], Mapping):
        raise WorkflowFailure(
            "material_clarification_contract_invalid:questions",
            failure_type="contract",
        )
    provider_options = tuple(
        str(item)
        for item in questions[0].get("options") or ()
        if isinstance(item, str) and item != CLARIFICATION_ESCAPE_OPTION
    )
    labels: list[str] = []
    actions: list[dict[str, Any]] = []
    for baseline_id, fallback_label, aliases in _BASELINE_CLARIFICATION_OPTIONS:
        label = next(
            (
                option
                for option in provider_options
                if any(alias in option for alias in aliases)
            ),
            fallback_label,
        )
        if label in labels:
            label = fallback_label
        if baseline_id == "previous_day" and "推荐" not in label:
            label = f"{label}（推荐）"
        labels.append(label)
        actions.append(
            {
                "choice_id": f"material-baseline-{baseline_id}",
                "action_kind": "bind_material_choice",
                "business_label": label,
                "material_patch": {
                    "baseline_candidates": [baseline_id],
                },
                "affected_material_slots": ["baseline"],
            }
        )
    escape_action = {
        "choice_id": "material-user-redirect",
        "action_kind": "user_redirect",
        "business_label": CLARIFICATION_ESCAPE_OPTION,
    }
    actions.append(escape_action)
    intent = state.get("intent") or {}
    ambiguous_slots = list(intent.get("ambiguous_slots") or ())
    if "baseline" not in _ambiguous_slot_names(intent):
        ambiguous_slots.append("baseline")
    state["intent"] = {
        **dict(intent),
        "ambiguous_slots": ambiguous_slots,
    }
    recommended_action = actions[0]
    return {
        **dict(output),
        "questions": [
            {
                "question": str(questions[0].get("question") or ""),
                "options": [*labels, CLARIFICATION_ESCAPE_OPTION],
            }
        ],
        "recommended_assumption": {
            "option": recommended_action["business_label"],
        },
        "recommended_choice_id": recommended_action["choice_id"],
        "choice_actions": actions,
    }


def _baseline_clarification_required(state: WorkflowState) -> bool:
    decision = state.get("boundary_decision") or {}
    if decision.get("boundary_status") != "needs_question":
        return False
    return _material_baseline_clarification_needed(state)


def _material_baseline_clarification_needed(state: WorkflowState) -> bool:
    intent = state.get("intent") or {}
    candidates = _canonical_baseline_ids(intent.get("baseline_candidates"))
    binding = intent.get("baseline_binding")
    if isinstance(binding, Mapping):
        if bool(binding.get("confirmed")) and candidates:
            return False
    elif candidates:
        return False
    families = _intent_question_family_set(intent)
    goal_ids = {
        str(binding.get("goal_id") or "")
        for binding in intent.get("goal_bindings") or ()
        if isinstance(binding, Mapping)
    }
    return bool(
        _ambiguous_slot_names(intent).intersection({"baseline", "baselines"})
        or intent.get("pattern_family") == "custom_baseline"
        or families.intersection(
            {
                "paid_amount_change_explanation",
                "custom_baseline_comparison",
                "revenue_health_review",
            }
        )
        or bool(
            goal_ids.intersection(
                {"explain_change", "validate_change", "compare_baseline"}
            )
        )
    )


def _persist_clarification(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "persist_clarification")
    material_slots = _intent_material_slots(state.get("intent") or {})
    package = {
        "run_id": state["run_id"],
        "status": "waiting_for_clarification",
        "clarification": to_jsonable(state.get("clarification_outcome") or {}),
        "accepted_graph": [],
        "analysis_route": {
            "requested_nodes": [],
            "analysis_requirements": material_slots,
        },
        "analysis_contract": {},
        "execution_material": None,
        "original_intent": to_jsonable(state.get("intent") or {}),
        "material_slots": to_jsonable(material_slots),
        "llm_calls": to_jsonable(state.get("llm_calls") or ()),
        "checkpoint_events": to_jsonable(
            state.get("checkpoint_events") or ()
        ),
    }
    state["workflow_status"] = "waiting_for_clarification"
    state["answer_package"] = package
    state["artifact_path"] = persist_artifact(
        package,
        artifact_root=state["request"].get("artifact_root", "artifacts/phase-4"),
    )
    return state


def _intent_material_slots(intent: Mapping[str, Any]) -> dict[str, Any]:
    slots: dict[str, Any] = {
        key: [] for key in MATERIAL_AUTHORITY_LIST_AXES
    }
    target_metric = str(intent.get("target_metric") or "").strip()
    if target_metric:
        slots["target_metrics"] = [target_metric]
    baselines = _canonical_baseline_ids(intent.get("baseline_candidates"))
    binding = intent.get("baseline_binding")
    if isinstance(binding, Mapping) and not bool(binding.get("confirmed")):
        baselines = []
    slots["baselines"] = list(dict.fromkeys(baselines))
    context_sources = [
        str(item)
        for item in intent.get("context_sources") or ()
        if isinstance(item, str) and item
    ]
    slots["context_sources"] = list(dict.fromkeys(context_sources))
    source_fields = {
        "claim_types": "publishable_claim_types",
        "dimension_ids": "dimension_ids",
        "component_ids": "component_ids",
        "association_metric_ids": "association_metric_ids",
        "required_outcomes": "required_outcomes",
        "analysis_axis_ids": "analysis_axis_ids",
    }
    for key, source_field in source_fields.items():
        values = [
            str(item)
            for item in intent.get(source_field) or ()
            if isinstance(item, str) and item
        ]
        slots[key] = list(dict.fromkeys(values))
    scope = intent.get("scope")
    if scope not in (None, "", {}, []):
        slots["scope"] = scope
    return slots


def _canonical_baseline_ids(raw: Any, *, strict: bool = False) -> list[str]:
    try:
        return list(canonical_baseline_ids(raw))
    except BaselineSemanticError as exc:
        if strict:
            raise WorkflowFailure(
                "clarification_attempt_source_material_conflict:baselines",
                failure_type="contract",
            ) from exc
        return []


def _local_clarification_question_output(state: WorkflowState) -> dict[str, Any]:
    questions = state.get("boundary_decision", {}).get("clarification_questions") or []
    if not questions:
        questions = _local_outlier_clarification_questions()
    return {
        "questions": questions,
        "recommended_assumption": state.get("boundary_decision", {}).get(
            "recommended_assumption"
        )
        or "采用产品默认业务假设继续，并把假设写入本次分析边界。",
        "status_message": "需要确认会改变业务结论的执行边界。",
    }


def _local_outlier_clarification_questions() -> list[dict[str, Any]]:
    return [
        {
            "question": "要按什么口径移除异常日期后复算？",
            "options": [
                "按日粒度，移除贡献最大的正向日期后复算。",
                "只标记异常日期，不从结果中移除。",
                "先不复算，继续保留原结论和异常风险提示。",
                CLARIFICATION_ESCAPE_OPTION,
            ],
        }
    ]


def _has_bound_intra_period_comparison(state: WorkflowState) -> bool:
    intent = state.get("intent", {})
    params = dict(intent.get("pattern_params", {}))
    if intent.get("pattern_family") != "intra_period" or not params.get("target_phase"):
        return False
    group_key = params.get("group_key", "phase")
    rows = state.get("request", {}).get("rows") or []
    groups = {row.get(group_key) for row in rows if row.get(group_key) is not None}
    siblings = groups - {params.get("target_phase")}
    if len(siblings) < 2:
        return False
    question = str(state.get("request", {}).get("question") or "")
    return any(token in question for token in ("月中", "月末", "mid", "end"))


def _rebind_after_clarification(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "rebind_after_clarification")
    choice = state["clarification_outcome"].get("choice", {})
    if isinstance(choice, dict):
        state["intent"].update(choice)
        if "question_family" in choice:
            family = choice["question_family"]
            state["intent"].update(
                {
                    "question_family": family,
                    "primary_question_family": family,
                    "question_families": [family],
                    "secondary_question_families": [],
                }
            )
        rebound_slots = {
            slot
            for field in choice
            for slot in _MATERIAL_CLARIFICATION_FIELD_SLOTS.get(field, set())
        }
        state["intent"]["ambiguous_slots"] = [
            item
            for item in state["intent"].get("ambiguous_slots", ())
            if str(item.get("slot") if isinstance(item, Mapping) else item)
            not in rebound_slots
        ]
        if choice:
            state["clarification_choice_consumed"] = True
    return state


def _confirm_business_understanding(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "confirm_business_understanding")
    required_machine_intent = _confirm_machine_intent_contract(state["intent"])
    output = _invoke_llm(
        state,
        "confirm_understanding",
        {
            "intent": state["intent"],
            "required_machine_intent": required_machine_intent,
            "boundary_decision": state["boundary_decision"],
            "clarification_outcome": state.get("clarification_outcome", {}),
        },
        output_validator=lambda value: _validate_confirm_understanding_provider_output(
            value,
            state["intent"],
        ),
    )
    state["confirmed_understanding"] = _normalize_confirm_understanding_output(
        output,
        state["intent"],
    )
    return state


def _validate_confirm_understanding_provider_output(
    output: Mapping[str, Any],
    intent: Mapping[str, Any],
) -> None:
    try:
        _normalize_confirm_understanding_output(output, intent)
    except WorkflowFailure as exc:
        raise LLMOutputError(_exception_reason(exc)) from exc


def _normalize_confirm_understanding_output(
    output: Mapping[str, Any],
    intent: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(output, Mapping):
        raise WorkflowFailure(
            "confirm_understanding_contract_invalid:output",
            failure_type="llm_contract",
        )
    confirmed_intent = output.get("confirmed_intent")
    if not isinstance(confirmed_intent, Mapping):
        raise WorkflowFailure(
            "confirm_understanding_contract_invalid:confirmed_intent",
            failure_type="llm_contract",
        )
    business_summary = confirmed_intent.get("business_summary")
    if (
        not isinstance(business_summary, str)
        or not business_summary
        or business_summary != business_summary.strip()
        or _has_internal_visible_token(business_summary)
    ):
        raise WorkflowFailure(
            "confirm_understanding_contract_invalid:confirmed_intent",
            failure_type="llm_contract",
        )
    required_machine_intent = _confirm_machine_intent_contract(intent)
    accepted_assumptions = output.get("accepted_assumptions")
    if (
        not isinstance(accepted_assumptions, list)
        or any(
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or not re.search(r"[\u4e00-\u9fff]", item)
            or _has_internal_visible_token(item)
            for item in accepted_assumptions
        )
    ):
        raise WorkflowFailure(
            "confirm_understanding_contract_invalid:accepted_assumptions",
            failure_type="llm_contract",
        )
    for field in ("status_message", "display_summary"):
        if field == "display_summary" and field not in output:
            continue
        value = output.get(field)
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or _has_internal_visible_token(value)
        ):
            raise WorkflowFailure(
                f"confirm_understanding_contract_invalid:{field}",
                failure_type="llm_contract",
            )
    return {
        **dict(output),
        "confirmed_intent": {
            **dict(confirmed_intent),
            "business_summary": business_summary,
            "machine_intent": required_machine_intent,
        },
        "accepted_assumptions": list(accepted_assumptions),
    }


def _confirm_machine_intent_contract(
    intent: Mapping[str, Any],
) -> dict[str, Any]:
    material_fields = (
        "question_family",
        "target_metric",
        "pattern_family",
        "scope",
        "time_window",
        "target_claim",
        "pattern_params",
        "baseline_candidates",
    )
    contract = {
        field: to_jsonable(intent[field])
        for field in material_fields
        if field in intent
    }
    contract.setdefault("baseline_candidates", [])
    for field in ("baseline", "target"):
        value = intent.get(field)
        if not _empty_business_context_value(value):
            contract[field] = to_jsonable(value)
    return contract


def _design_analysis_route(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "design_analysis_route")
    budget = state.get("budget_state") or default_budget("ordinary")
    state["budget_state"] = budget
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    allowed_baseline_ids = list(CURRENT_DATA_BASELINES)
    capability_cards = _route_capability_cards()
    route_payload = {
        "intent": state["intent"],
        "confirmed_understanding": state["confirmed_understanding"],
        "known_capabilities": capability_cards,
        "allowed_dataset_ids": registry.dataset_ids,
        "allowed_context_source_ids": registry.context_source_ids,
        "allowed_diagnostic_ids": registry.diagnostic_obligation_ids,
        "allowed_baseline_ids": allowed_baseline_ids,
        "budget_state": budget.to_llm_summary(),
        "diagnostic_insights": to_jsonable(
            state.get("diagnostic_insights") or {}
        ),
        "requested_diagnostic_route": str(
            state.get("next_action", {}).get("diagnostic_route") or ""
        ),
    }
    required_capabilities = _deterministic_required_route_capabilities(
        state,
        registry,
    )
    provider_capability_ids = frozenset(
        str(card.get("capability_id") or "")
        for card in capability_cards
        if str(card.get("capability_id") or "")
    )
    provider_required_capabilities = tuple(
        capability
        for capability in required_capabilities
        if capability in provider_capability_ids
    )
    allowed_provider_capability_ids = provider_capability_ids
    known_capability_ids = frozenset(registry.capability_ids)
    route_payload["required_capability_ids"] = list(
        provider_required_capabilities
    )
    route_payload["required_capabilities"] = [
        dict(card)
        for card in capability_cards
        if str(card.get("capability_id") or "")
        in set(provider_required_capabilities)
    ]
    output = _invoke_llm(
        state,
        "analysis_route_plan",
        route_payload,
        output_validator=(
            lambda candidate: _validate_analysis_route_proposal(
                candidate,
                allowed_provider_capability_ids,
                registry,
            )
        ),
    )
    output = {
        key: output.get(key)
        for key in ("requested_nodes", "analysis_requirements")
        if key in output
    }
    _validate_analysis_route_proposal(
        output,
        allowed_provider_capability_ids,
        registry,
    )
    output, material_conflicts = _merge_confirmed_material_requirements(
        output,
        state,
    )
    _validate_route_analysis_requirements(output, registry)
    state["route_material_conflicts"] = material_conflicts
    if material_conflicts:
        state["boundary_decision"] = {
            "boundary_status": "needs_question",
            "recommended_assumption": "保留已确认的指标、基线和背景范围继续。",
            "clarification_questions": [],
            "decision_summary": "分析路线与已确认业务边界冲突，需要用户确认。",
        }
    requested = _requested_node_ids(
        output.get("requested_nodes"),
        excluded=ROUTE_BLOCKED_CAPABILITY_IDS,
    )
    if not requested:
        requested = ("pattern_scan",)
    requested, output = reconcile_analysis_route(
        requested,
        output,
        state["intent"],
        RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH),
        trusted_prior_route=_trusted_obligation_rejection_route(state),
    )
    _consume_obligation_route_conflict(state, output)
    output = _finalize_production_analysis_route_narrative(
        state,
        route=output,
        requested=requested,
        registry=registry,
    )
    state["analysis_route"] = {**output, "requested_nodes": requested}
    _validate_final_analysis_route_mapping(
        state["analysis_route"],
        requested=requested,
        known_capability_ids=known_capability_ids,
    )
    _record_obligation_rejection_authority(state, state["analysis_route"])
    state["intent"]["requested_nodes"] = requested
    return state


def _validate_analysis_route_provider_output(
    output: Mapping[str, Any],
    known_capability_ids: frozenset[str],
    *,
    expected_requested_nodes: Sequence[str] | None = None,
    allow_empty: bool = False,
    forbidden_narrative_terms: Sequence[str] = (),
    require_capability_sections: bool = False,
    expected_evidence_from_capability_sections: bool = False,
) -> None:
    requested = output.get("requested_nodes")
    if (
        not isinstance(requested, (list, tuple))
        or (not requested and not allow_empty)
        or any(
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or item not in known_capability_ids
            for item in requested
        )
        or len(requested) != len(set(requested))
        or (
            expected_requested_nodes is not None
            and tuple(requested) != tuple(expected_requested_nodes)
        )
    ):
        raise LLMOutputError(
            "analysis_route_provider_contract_invalid:requested_nodes"
        )
    for field in ("route_summary", "decision_summary", "display_summary"):
        value = output.get(field)
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or _route_narrative_has_machine_id(value, known_capability_ids)
            or any(term and term in value for term in forbidden_narrative_terms)
            or _has_internal_visible_token(value)
        ):
            raise LLMOutputError(
                f"analysis_route_provider_contract_invalid:{field}"
            )
    if not expected_evidence_from_capability_sections:
        expected_evidence = output.get("expected_evidence")
        if (
            not isinstance(expected_evidence, Mapping)
            or set(expected_evidence) != set(requested)
            or any(
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or not re.search(r"[\u3400-\u9fff]", value)
                or _route_narrative_has_machine_id(value, known_capability_ids)
                or any(
                    term and term in value
                    for term in forbidden_narrative_terms
                )
                or _has_internal_visible_token(value)
                for value in expected_evidence.values()
            )
        ):
            raise LLMOutputError(
                "analysis_route_provider_contract_invalid:expected_evidence"
            )
    if require_capability_sections:
        _validate_route_capability_sections(
            output,
            requested=tuple(requested),
            known_capability_ids=known_capability_ids,
            forbidden_narrative_terms=forbidden_narrative_terms,
            compare_expected_evidence=(
                not expected_evidence_from_capability_sections
            ),
        )


def _validate_analysis_route_proposal(
    output: Mapping[str, Any],
    known_capability_ids: frozenset[str],
    registry: RuntimeContractRegistry,
) -> None:
    requested = output.get("requested_nodes")
    if (
        not isinstance(requested, (list, tuple))
        or not requested
        or any(
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or item not in known_capability_ids
            for item in requested
        )
        or len(requested) != len(set(requested))
    ):
        raise LLMOutputError(
            "analysis_route_provider_contract_invalid:requested_nodes"
        )
    requirements = output.get("analysis_requirements")
    if isinstance(requirements, Mapping) and set(requirements).intersection(
        {
            "component_ids",
            "association_metric_ids",
            "dimension_ids",
            "claim_types",
            "required_outcomes",
            "analysis_axis_ids",
        }
    ):
        raise LLMOutputError(
            "analysis_route_provider_contract_invalid:locally_owned_analysis_plan"
        )
    try:
        _validate_route_analysis_requirements(output, registry)
    except WorkflowFailure as exc:
        raise LLMOutputError(_exception_reason(exc)) from exc


def _validate_route_capability_sections(
    output: Mapping[str, Any],
    *,
    requested: Sequence[str],
    known_capability_ids: frozenset[str],
    forbidden_narrative_terms: Sequence[str] = (),
    compare_expected_evidence: bool = True,
) -> None:
    sections = output.get("capability_sections")
    if not isinstance(sections, Mapping) or set(sections) != set(requested):
        raise LLMOutputError(
            "analysis_route_provider_contract_invalid:capability_sections"
        )
    normalized_expected: dict[str, str] = {}
    for capability_id in requested:
        section = sections.get(capability_id)
        if (
            not isinstance(section, Mapping)
            or set(section) != _ROUTE_CAPABILITY_SECTION_FIELDS
        ):
            raise LLMOutputError(
                "analysis_route_provider_contract_invalid:capability_sections"
            )
        for field in _ROUTE_CAPABILITY_SECTION_FIELDS:
            value = section.get(field)
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or not re.search(r"[\u3400-\u9fff]", value)
                or _route_narrative_has_machine_id(
                    value, known_capability_ids
                )
                or any(
                    term and term in value
                    for term in forbidden_narrative_terms
                )
                or _has_internal_visible_token(value)
            ):
                raise LLMOutputError(
                    "analysis_route_provider_contract_invalid:"
                    "capability_sections"
                )
        normalized_expected[capability_id] = str(
            section["expected_evidence"]
        )
    if compare_expected_evidence and canonical_value(
        output.get("expected_evidence")
    ) != canonical_value(normalized_expected):
        raise LLMOutputError(
            "analysis_route_provider_contract_invalid:capability_sections"
        )


def _validated_analysis_route_provider_output(
    output: Mapping[str, Any],
    known_capability_ids: frozenset[str],
    *,
    require_capability_sections: bool = False,
) -> dict[str, Any]:
    try:
        _validate_analysis_route_provider_output(
            output,
            known_capability_ids,
            require_capability_sections=require_capability_sections,
        )
    except LLMOutputError as exc:
        raise WorkflowFailure(
            str(exc),
            failure_type="llm_contract",
        ) from exc
    return dict(output)


def _deterministic_required_route_capabilities(
    state: WorkflowState,
    registry: RuntimeContractRegistry,
) -> tuple[str, ...]:
    seed_route = {
        "analysis_requirements": _intent_material_slots(state.get("intent") or {})
    }
    requested, _ = reconcile_analysis_route(
        (),
        seed_route,
        state.get("intent") or {},
        registry,
        trusted_prior_route=_trusted_obligation_rejection_route(state),
    )
    return requested


def _finalize_production_analysis_route_narrative(
    state: WorkflowState,
    *,
    route: Mapping[str, Any],
    requested: tuple[str, ...],
    registry: RuntimeContractRegistry,
) -> dict[str, Any]:
    requested_set = set(requested)
    cards_by_id = {
        str(card.get("capability_id") or ""): dict(card)
        for card in _route_capability_cards(include_blocked=True)
        if str(card.get("capability_id") or "")
    }
    capability_cards = [
        cards_by_id[capability_id]
        for capability_id in requested
        if capability_id in cards_by_id
    ]
    if {card["capability_id"] for card in capability_cards} != requested_set:
        raise WorkflowFailure(
            "analysis_route_provider_contract_invalid:required_capabilities",
            failure_type="llm_contract",
        )
    payload, step_bindings = _build_final_route_narrative_payload(
        state,
        requested=requested,
        route=route,
        capability_cards=capability_cards,
        registry=registry,
    )
    step_refs = tuple(step_bindings)

    def validate(candidate: Mapping[str, Any]) -> None:
        _validate_final_route_narrative_output(
            candidate,
            expected_step_refs=step_refs,
            forbidden_machine_terms=(
                *registry.capability_ids,
                *registry.metric_ids,
                *registry.dataset_ids,
                *registry.dimension_ids,
                *registry.diagnostic_obligation_ids,
                *CURRENT_DATA_BASELINES,
            ),
        )
    try:
        narrative = _invoke_llm(
            state,
            "final_route_narrative",
            payload,
            output_validator=validate,
        )
        validate(narrative)
    except WorkflowFailure as exc:
        output = _without_route_narrative_fields(route)
        output.update(
            {
                "route_narrative_status": "unavailable",
                "route_narrative_failure": str(exc),
            }
        )
        return output
    return _project_final_analysis_route_narrative(
        route,
        narrative,
        requested=requested,
        step_bindings=step_bindings,
    )


def _build_final_route_narrative_payload(
    state: Mapping[str, Any],
    *,
    requested: Sequence[str],
    route: Mapping[str, Any] | None = None,
    capability_cards: Sequence[Mapping[str, Any]] | None = None,
    registry: RuntimeContractRegistry | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    registry = registry or RuntimeContractRegistry.from_path(
        CANONICAL_RUNTIME_BINDINGS_PATH
    )
    cards = capability_cards or _route_capability_cards(include_blocked=True)
    cards_by_id = {
        str(card.get("capability_id") or ""): dict(card)
        for card in cards
        if str(card.get("capability_id") or "")
    }
    step_bindings = {
        f"step_{index}": capability_id
        for index, capability_id in enumerate(requested, start=1)
    }
    route_steps = []
    for step_ref, capability_id in step_bindings.items():
        card = cards_by_id.get(capability_id) or {}
        business_name = str(card.get("business_name") or "").strip()
        if not business_name or not re.search(r"[\u3400-\u9fff]", business_name):
            raise WorkflowFailure(
                "analysis_route_narrative_business_label_missing",
                failure_type="contract",
            )
        claim_labels = [
            _CLAIM_INTENT_BUSINESS_SEMANTICS[claim_type]
            for claim_type in card.get("allowed_claim_types") or ()
            if claim_type in _CLAIM_INTENT_BUSINESS_SEMANTICS
        ]
        purpose = (
            "用于核对" + "、".join(dict.fromkeys(claim_labels))
            if claim_labels
            else f"用于完成{business_name}"
        )
        route_steps.append(
            {
                "step_ref": step_ref,
                "business_name": business_name,
                "purpose": purpose,
                "role": _route_narrative_step_role(
                    capability_id,
                    route or {},
                ),
            }
        )
    intent = state.get("intent") or {}
    metric_id = str(intent.get("target_metric") or "")
    try:
        metric_label = registry.metric_business_labels(metric_id)[0]
    except (IndexError, KeyError):
        metric_label = "目标指标"
    payload = {
        "route_context": {
            "metric": metric_label,
            "target": _route_narrative_target_label(intent),
            "baseline": _route_narrative_baseline_label(intent),
            "direction_status": _route_narrative_direction_status(state),
            "route_steps": route_steps,
        }
    }
    return payload, step_bindings


def _route_narrative_target_label(intent: Mapping[str, Any]) -> str:
    value = str(
        intent.get("target_semantic")
        or intent.get("time_window")
        or ""
    ).strip()
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return "已确认的目标时间"
    return f"{parsed.year}年{parsed.month}月{parsed.day}日"


def _route_narrative_baseline_label(intent: Mapping[str, Any]) -> str:
    binding = intent.get("baseline_binding") or {}
    candidates = (
        binding.get("candidates")
        if isinstance(binding, Mapping)
        else ()
    ) or intent.get("baseline_candidates") or ()
    baseline_id = str(next(iter(candidates), ""))
    return {
        "previous_day": "前一天",
        "rolling_7_day_baseline": "近7日均值",
        "same_weekday_last_week": "上周同日",
    }.get(baseline_id, "已确认的比较基准")


def _route_narrative_direction_status(state: Mapping[str, Any]) -> str:
    intent = state.get("intent") or {}
    request = state.get("request") or {}
    material = " ".join(
        str(value or "")
        for value in (
            intent.get("target_claim"),
            request.get("question"),
        )
    )
    increase = any(
        token in material
        for token in ("上涨", "上升", "增长", "增加", "提升")
    )
    decrease = any(
        token in material
        for token in ("下跌", "下降", "下滑", "减少", "降低")
    )
    if increase and not decrease:
        return "用户提出的上涨仍待数据验证"
    if decrease and not increase:
        return "用户提出的下跌仍待数据验证"
    return "变化方向待数据验证"


def _route_narrative_step_role(
    capability_id: str,
    route: Mapping[str, Any],
) -> str:
    resolution = route.get("obligation_resolution") or {}
    mutations = (
        resolution.get("mutations")
        if isinstance(resolution, Mapping)
        else ()
    ) or ()
    for mutation in mutations:
        if not isinstance(mutation, Mapping):
            continue
        if str(mutation.get("capability") or "") != capability_id:
            continue
        if str(mutation.get("reason") or "").startswith("auxiliary_"):
            return "辅助步骤"
    return "核心步骤"


def _validate_final_route_narrative_output(
    output: Mapping[str, Any],
    *,
    expected_step_refs: Sequence[str],
    forbidden_machine_terms: Sequence[str],
) -> None:
    def validate_prose(field: str, value: Any) -> None:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or not re.search(r"[\u3400-\u9fff]", value)
            or _has_internal_visible_token(value)
            or any(
                term and _narrative_contains_machine_term(value, term)
                for term in forbidden_machine_terms
            )
        ):
            raise LLMOutputError(
                f"final_route_narrative_invalid:{field}"
            )

    for field in ("route_summary", "decision_summary", "display_summary"):
        validate_prose(field, output.get(field))
    sections = output.get("sections")
    if not isinstance(sections, (list, tuple)) or len(sections) != len(
        expected_step_refs
    ):
        raise LLMOutputError("final_route_narrative_invalid:sections")
    for index, (section, expected_ref) in enumerate(
        zip(sections, expected_step_refs, strict=True)
    ):
        if (
            not isinstance(section, Mapping)
            or set(section) != {"step_ref", "route_step", "expected_evidence"}
            or section.get("step_ref") != expected_ref
        ):
            raise LLMOutputError("final_route_narrative_invalid:sections")
        validate_prose(f"sections[{index}].route_step", section.get("route_step"))
        validate_prose(
            f"sections[{index}].expected_evidence",
            section.get("expected_evidence"),
        )


def _narrative_contains_machine_term(text: str, term: str) -> bool:
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(str(term))}(?![A-Za-z0-9_])",
            text,
            flags=re.IGNORECASE,
        )
    )


def _without_route_narrative_fields(route: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(route)
    for field in (
        "route_overview",
        "route_summary",
        "expected_evidence",
        "decision_summary",
        "display_summary",
        "capability_sections",
        "narrative_capability_refs",
        "narrative_authority",
        "route_narrative_status",
        "route_narrative_failure",
    ):
        output.pop(field, None)
    return output


def _project_final_analysis_route_narrative(
    route: Mapping[str, Any],
    narrative: Mapping[str, Any],
    *,
    requested: Sequence[str],
    step_bindings: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    output = _without_route_narrative_fields(route)
    step_bindings = step_bindings or {
        f"step_{index}": capability_id
        for index, capability_id in enumerate(requested, start=1)
    }
    sections_by_ref = {
        str(section["step_ref"]): section
        for section in narrative["sections"]
    }
    sections = {
        capability_id: {
            "route_step": str(sections_by_ref[step_ref]["route_step"]),
            "expected_evidence": str(
                sections_by_ref[step_ref]["expected_evidence"]
            ),
        }
        for step_ref, capability_id in step_bindings.items()
    }
    overview = str(narrative["route_summary"])
    route_parts = [
        overview,
        *(
            str(sections[capability_id]["route_step"])
            for capability_id in requested
        ),
    ]
    output.update(
        {
            "route_overview": overview,
            "route_summary": "\n".join(route_parts),
            "expected_evidence": {
                capability_id: str(
                    sections[capability_id]["expected_evidence"]
                )
                for capability_id in requested
            },
            "decision_summary": str(narrative["decision_summary"]),
            "display_summary": str(narrative["display_summary"]),
            "capability_sections": sections,
            "narrative_capability_refs": (
                _final_narrative_capability_refs(requested)
            ),
            "narrative_authority": _final_narrative_authority(),
            "route_narrative_status": "available",
        }
    )
    return output


def _final_narrative_capability_refs(
    requested: Sequence[str],
) -> dict[str, Any]:
    capability_ids = list(requested)
    return {
        "route_summary_capability_ids": capability_ids,
        "decision_summary_capability_ids": capability_ids,
        "display_summary_capability_ids": capability_ids,
        "expected_evidence_capability_ids": {
            capability_id: [capability_id]
            for capability_id in capability_ids
        },
    }


def _final_narrative_authority() -> dict[str, Any]:
    return {
        "schema_version": "analysis_route_narrative.v1",
        "authority_level": "display_advisory",
        "hard_authority_fields": list(
            _ROUTE_NARRATIVE_HARD_AUTHORITY_FIELDS
        ),
        "advisory_fields": list(_ROUTE_NARRATIVE_ADVISORY_FIELDS),
    }


def _validate_final_analysis_route_mapping(
    route: Mapping[str, Any],
    *,
    requested: Sequence[str],
    known_capability_ids: frozenset[str],
    allow_empty: bool = False,
) -> None:
    try:
        if route.get("route_narrative_status") == "unavailable":
            route_nodes = route.get("requested_nodes")
            if (
                not isinstance(route_nodes, (list, tuple))
                or tuple(route_nodes) != tuple(requested)
                or any(node not in known_capability_ids for node in route_nodes)
                or not isinstance(route.get("analysis_requirements"), Mapping)
                or not isinstance(route.get("route_narrative_failure"), str)
                or not route.get("route_narrative_failure")
                or any(
                    field in route
                    for field in (
                        "route_overview",
                        "route_summary",
                        "expected_evidence",
                        "decision_summary",
                        "display_summary",
                        "capability_sections",
                        "narrative_capability_refs",
                        "narrative_authority",
                    )
                )
            ):
                raise LLMOutputError(
                    "analysis_route_provider_contract_invalid:"
                    "route_narrative_unavailable"
                )
            return
        _validate_analysis_route_provider_output(
            route,
            known_capability_ids,
            expected_requested_nodes=requested,
            allow_empty=allow_empty,
            require_capability_sections=True,
        )
        if canonical_value(route.get("narrative_capability_refs")) != (
            canonical_value(_final_narrative_capability_refs(requested))
        ):
            raise LLMOutputError(
                "analysis_route_provider_contract_invalid:"
                "narrative_capability_refs"
            )
        if canonical_value(route.get("narrative_authority")) != canonical_value(
            _final_narrative_authority()
        ):
            raise LLMOutputError(
                "analysis_route_provider_contract_invalid:narrative_authority"
            )
        overview = route.get("route_overview")
        if (
            not isinstance(overview, str)
            or not overview
            or overview != overview.strip()
        ):
            raise LLMOutputError(
                "analysis_route_provider_contract_invalid:route_overview"
            )
        sections = route.get("capability_sections") or {}
        expected_summary = "\n".join(
            [
                overview,
                *(
                    str(sections[capability_id]["route_step"])
                    for capability_id in requested
                ),
            ]
        )
        if route.get("route_summary") != expected_summary:
            raise LLMOutputError(
                "analysis_route_provider_contract_invalid:route_summary_projection"
            )
        if route.get("route_narrative_status") != "available":
            raise LLMOutputError(
                "analysis_route_provider_contract_invalid:route_narrative_status"
            )
    except LLMOutputError as exc:
        raise WorkflowFailure(str(exc), failure_type="llm_contract") from exc


def _route_narrative_has_machine_id(
    value: str,
    known_capability_ids: frozenset[str],
) -> bool:
    return any(capability_id in value for capability_id in known_capability_ids)


def _validate_route_analysis_requirements(
    route: Mapping[str, Any], registry: RuntimeContractRegistry
) -> None:
    raw = route.get("analysis_requirements")
    if not isinstance(raw, Mapping):
        raise WorkflowFailure(
            "analysis_route_contract_invalid:analysis_requirements",
            failure_type="llm_contract",
        )
    claims = {
        str(claim)
        for capability_id in registry.capability_ids
        for claim in registry.capability_inputs(capability_id).get(
            "supported_claim_types", ()
        )
    }
    contracts = {
        "target_metrics": set(registry.metric_ids),
        "component_ids": set(registry.metric_ids),
        "association_metric_ids": set(registry.metric_ids),
        "dimension_ids": set(registry.dimension_ids),
        "claim_types": claims,
        "required_outcomes": {
            str(outcome)
            for goal_id in registry.analysis_goal_ids
            for outcome in registry.analysis_goal_obligation(goal_id).get(
                "required_outcomes", ()
            )
        },
        "analysis_axis_ids": set(registry.analysis_axis_ids),
        "context_sources": set(registry.context_source_ids),
        "diagnostic_tags": set(registry.diagnostic_obligation_ids),
        "dataset_requirements": set(registry.dataset_ids),
    }
    if set(raw) - {*contracts, "baselines", "context_window_specs", "scope"}:
        raise WorkflowFailure(
            "analysis_route_contract_invalid:analysis_requirements:unknown_fields",
            failure_type="llm_contract",
        )
    for key, allowed in contracts.items():
        if key not in raw:
            continue
        values = raw[key]
        if (
            not isinstance(values, (list, tuple))
            or any(
                not isinstance(item, str) or not item.strip()
                for item in values
            )
            or len(values) != len(set(values))
            or any(item not in allowed for item in values)
        ):
            raise WorkflowFailure(
                f"analysis_route_contract_invalid:analysis_requirements:{key}",
                failure_type="llm_contract",
            )
    if "baselines" in raw:
        baseline_values = raw["baselines"]
        if (
            not isinstance(baseline_values, (list, tuple))
            or any(
                not isinstance(item, str) or not item.strip()
                for item in baseline_values
            )
            or len(baseline_values) != len(set(baseline_values))
            or _canonical_baseline_ids(baseline_values) != list(baseline_values)
        ):
            raise WorkflowFailure(
                "analysis_route_contract_invalid:analysis_requirements:baselines",
                failure_type="llm_contract",
            )
    _validate_route_context_window_specs(
        raw.get("context_window_specs", ()),
        requested_capabilities=tuple(route.get("requested_nodes") or ()),
        registry=registry,
    )


def _validate_route_context_window_specs(
    raw_specs: Any,
    *,
    requested_capabilities: Sequence[str],
    registry: RuntimeContractRegistry,
) -> None:
    if not isinstance(raw_specs, (list, tuple)):
        raise WorkflowFailure(
            "analysis_route_contract_invalid:analysis_requirements:"
            "context_window_specs",
            failure_type="llm_contract",
        )
    requested = {str(item) for item in requested_capabilities if str(item)}
    seen_capabilities: set[str] = set()
    for index, raw in enumerate(raw_specs):
        if not isinstance(raw, Mapping) or set(raw) != {
            "capability_id",
            "relation",
            "unit",
            "count",
        }:
            raise WorkflowFailure(
                "analysis_route_contract_invalid:analysis_requirements:"
                f"context_window_specs:{index}:shape",
                failure_type="llm_contract",
            )
        capability_id = str(raw.get("capability_id") or "")
        relation = str(raw.get("relation") or "")
        unit = str(raw.get("unit") or "")
        count = raw.get("count")
        try:
            policy = registry.capability_inputs(capability_id).get(
                "context_window_policy"
            )
        except KeyError:
            policy = None
        bounds = (
            policy.get("count_bounds")
            if isinstance(policy, Mapping)
            else None
        )
        unit_bounds = bounds.get(unit) if isinstance(bounds, Mapping) else None
        valid_count = (
            isinstance(count, int)
            and not isinstance(count, bool)
            and isinstance(unit_bounds, (list, tuple))
            and len(unit_bounds) == 2
            and unit_bounds[0] <= count <= unit_bounds[1]
        )
        if (
            capability_id not in requested
            or not isinstance(policy, Mapping)
            or relation != policy.get("relation")
            or unit not in set(policy.get("allowed_units") or ())
            or not valid_count
            or capability_id in seen_capabilities
        ):
            raise WorkflowFailure(
                "analysis_route_contract_invalid:analysis_requirements:"
                f"context_window_specs:{index}:policy",
                failure_type="llm_contract",
            )
        seen_capabilities.add(capability_id)


def _merge_confirmed_material_requirements(
    route: Mapping[str, Any],
    state: WorkflowState,
    *,
    strict_resume_authority: bool = False,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    output = dict(route)
    raw_proposed = output.get("analysis_requirements")
    if not isinstance(raw_proposed, Mapping):
        return output, ()
    proposed = dict(raw_proposed)
    # Clarification artifacts preserve the source run for audit and tamper
    # detection. Executable axes are recompiled from the currently bound intent
    # so a user-confirmed baseline does not freeze an older registry's derived
    # capabilities, dimensions, or claim roles into a later attempt.
    confirmed = _intent_material_slots(state.get("intent") or {})
    authoritative: dict[str, Any] = dict(confirmed)
    conflicts: list[str] = []
    confirmed_targets = _typed_material_axis_values(
        confirmed.get("target_metrics")
    )
    proposed_targets = _typed_material_axis_values(
        proposed.get("target_metrics")
    )
    if (
        confirmed_targets
        and proposed_targets
        and any(item not in proposed_targets for item in confirmed_targets)
    ):
        conflicts.append("target_metrics")
    confirmed_scope = confirmed.get("scope")
    if (
        confirmed_scope not in (None, "", {}, [])
        and proposed.get("scope") not in (None, "", {}, [])
        and _material_scope_signature(proposed.get("scope"))
        != _material_scope_signature(confirmed_scope)
    ):
        conflicts.append("scope")
    for key in MATERIAL_AUTHORITY_LIST_AXES:
        proposed_values = _typed_material_axis_values(proposed.get(key))
        if (
            key == "baselines"
            and isinstance(
                (state.get("intent") or {}).get("baseline_binding"),
                Mapping,
            )
            and not bool(
                (state.get("intent") or {})["baseline_binding"].get(
                    "confirmed"
                )
            )
        ):
            if proposed_values:
                conflicts.append("baselines")
            proposed[key] = []
            continue
        if strict_resume_authority and key in proposed:
            authoritative_values = _typed_material_axis_values(
                authoritative.get(key)
            )
            if (
                key not in authoritative
                or proposed_values is None
                or authoritative_values is None
                or set(proposed_values) != set(authoritative_values)
            ):
                conflicts.append(key)
        confirmed_values = _typed_material_axis_values(confirmed.get(key))
        if confirmed_values is None:
            if key in confirmed:
                proposed[key] = confirmed[key]
            continue
        if not confirmed_values:
            if key in confirmed and key not in proposed:
                proposed[key] = []
            continue
        if proposed_values is None:
            if key not in proposed:
                proposed[key] = list(confirmed_values)
            continue
        if len(confirmed_values) != len(set(confirmed_values)):
            proposed[key] = list(confirmed_values)
            continue
        if len(proposed_values) != len(set(proposed_values)):
            proposed[key] = list(proposed_values)
            continue
        confirmed_set = set(confirmed_values)
        proposed[key] = [
            *confirmed_values,
            *(
                item
                for item in proposed_values
                if item not in confirmed_set
            ),
        ]
    if confirmed_scope not in (None, "", {}, []):
        proposed["scope"] = confirmed_scope
    output["analysis_requirements"] = proposed
    return output, tuple(dict.fromkeys(conflicts))


def _typed_material_axis_values(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        return None
    return tuple(value)


def _claim_intent_values(*values: Any) -> tuple[str, ...]:
    normalized: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, str):
            item = value.strip()
            if re.fullmatch(r"[a-z][a-z0-9_]*", item):
                normalized.append(item)
            return
        if isinstance(value, Mapping):
            for key in (
                "claim_types",
                "claim_intents",
                "claim_type",
                "claim_intent",
            ):
                if key in value:
                    visit(value[key])
            return
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            for item in value:
                visit(item)

    for value in values:
        visit(value)
    return tuple(dict.fromkeys(normalized))


def _reconcile_route_input_capabilities(
    requested: tuple[str, ...],
    route: Mapping[str, Any],
    intent: Mapping[str, Any],
    registry: RuntimeContractRegistry,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    output = dict(route)
    requirements = dict(output.get("analysis_requirements") or {})
    target_metrics = tuple(
        dict.fromkeys(
            str(metric)
            for metric in (
                requirements.get("target_metrics")
                or (intent.get("target_metric"),)
            )
            if str(metric)
        )
    )
    if not target_metrics:
        return requested, output

    comparative_required = bool(requirements.get("baselines"))

    def covers(capability_id: str, metric_id: str) -> bool:
        try:
            contract = registry.capability_inputs(capability_id)
        except KeyError:
            return False
        if not contract.get("query_families"):
            return False
        if str(contract.get("metric_mode") or "") == "requested":
            metrics = contract.get("allowed_metrics") or ()
        else:
            metrics = (
                *(contract.get("required_metrics") or ()),
                *(contract.get("optional_metrics") or ()),
            )
        if metric_id not in metrics:
            return False
        if comparative_required and "comparative_change" not in set(
            contract.get("supported_claim_types") or ()
        ):
            return False
        return True

    additions: list[str] = []
    for metric_id in target_metrics:
        if any(covers(capability_id, metric_id) for capability_id in requested):
            continue
        candidates = []
        for card in llm_capability_cards():
            capability_id = str(card.get("capability_id") or "")
            if not capability_id or capability_id in ROUTE_BLOCKED_CAPABILITY_IDS:
                continue
            try:
                contract = registry.capability_inputs(capability_id)
            except KeyError:
                continue
            if not covers(capability_id, metric_id):
                continue
            supported_claims = set(contract.get("supported_claim_types") or ())
            if comparative_required and "comparative_change" not in supported_claims:
                continue
            candidates.append(capability_id)
        if len(candidates) == 1:
            additions.append(candidates[0])

    context_sources = tuple(
        str(source)
        for source in requirements.get("context_sources") or ()
        if str(source)
    )
    context_additions: list[str] = []
    selected_with_additions = tuple(dict.fromkeys((*additions, *requested)))
    if context_sources:
        def covers_context(capability_id: str) -> bool:
            try:
                contract = registry.capability_inputs(capability_id)
            except KeyError:
                return False
            return (
                str(contract.get("source_mode") or "")
                == "requested_context_sources"
                and bool(contract.get("query_families"))
            )

        if not any(covers_context(capability) for capability in selected_with_additions):
            intent_families = _intent_question_family_set(intent)
            context_candidates = []
            for card in llm_capability_cards():
                capability_id = str(card.get("capability_id") or "")
                if not capability_id or not covers_context(capability_id):
                    continue
                supported_families = set(card.get("supported_question_families") or ())
                if intent_families and not intent_families.intersection(supported_families):
                    continue
                context_candidates.append(capability_id)
            if len(context_candidates) == 1:
                context_additions.append(context_candidates[0])

    if not additions and not context_additions:
        return requested, output
    reconciled = tuple(
        dict.fromkeys((*additions, *requested, *context_additions))
    )
    requirements["target_metrics"] = list(target_metrics)
    claim_types = list(requirements.get("claim_types") or ())
    if comparative_required and "comparative_change" not in claim_types:
        claim_types.append("comparative_change")
    requirements["claim_types"] = claim_types
    output["analysis_requirements"] = requirements
    return reconciled, output


def _reconcile_route_metric_capabilities(
    requested: tuple[str, ...],
    route: Mapping[str, Any],
    intent: Mapping[str, Any],
    registry: RuntimeContractRegistry,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Compatibility wrapper for metric/source input reconciliation."""
    return _reconcile_route_input_capabilities(requested, route, intent, registry)


def _validated_obligation_rejection_history(
    route: Mapping[str, Any],
) -> tuple[MutationRecord, ...]:
    resolution = route.get("obligation_resolution")
    if resolution in (None, {}):
        return ()
    if not isinstance(resolution, Mapping):
        raise WorkflowFailure(
            "analysis_route_contract_invalid:obligation_mutation_history",
            failure_type="contract",
        )
    has_history = "mutation_history" in resolution
    if not has_history and resolution.get("status") != "resolved":
        return ()
    raw_mutations = resolution.get(
        "mutation_history" if has_history else "mutations", ()
    )
    if not isinstance(raw_mutations, (list, tuple)):
        raise WorkflowFailure(
            "analysis_route_contract_invalid:obligation_mutation_history",
            failure_type="contract",
        )
    records: list[MutationRecord] = []
    for raw in raw_mutations:
        if not isinstance(raw, Mapping):
            raise WorkflowFailure(
                "analysis_route_contract_invalid:obligation_mutation_history",
                failure_type="contract",
            )
        action = raw.get("action")
        if not has_history and action != "rejected":
            continue
        capability = raw.get("capability")
        reason = raw.get("reason")
        if not all(
            isinstance(item, str)
            and item
            and item == item.strip()
            for item in (action, capability, reason)
        ):
            raise WorkflowFailure(
                "analysis_route_contract_invalid:obligation_mutation_history",
                failure_type="contract",
            )
        if (
            set(raw) != {"action", "capability", "reason"}
            or action != "rejected"
            or reason not in _LOCAL_OBLIGATION_REJECTION_REASONS
        ):
            raise WorkflowFailure(
                "analysis_route_contract_invalid:obligation_mutation_history",
                failure_type="contract",
            )
        record = MutationRecord(
            action=action,
            capability=capability,
            reason=reason,
        )
        if record not in records:
            records.append(record)
    return tuple(records)


def _merge_obligation_rejection_history(
    carried: Sequence[MutationRecord],
    mutations: Sequence[Mapping[str, Any]],
) -> tuple[MutationRecord, ...]:
    rejected: list[dict[str, Any]] = []
    for mutation in mutations:
        if not isinstance(mutation, Mapping):
            raise WorkflowFailure(
                "analysis_route_contract_invalid:obligation_mutation_history",
                failure_type="contract",
            )
        if mutation.get("action") == "rejected":
            rejected.append(dict(mutation))
    candidate_route = {
        "obligation_resolution": {
            "mutation_history": rejected
        }
    }
    merged = list(carried)
    for record in _validated_obligation_rejection_history(candidate_route):
        if record not in merged:
            merged.append(record)
    return tuple(merged)


def _obligation_rejection_payload(
    records: Sequence[MutationRecord],
) -> list[dict[str, str]]:
    return [
        {
            "action": record.action,
            "capability": record.capability,
            "reason": record.reason,
        }
        for record in records
    ]


def _trusted_obligation_rejection_route(
    state: Mapping[str, Any],
) -> dict[str, Any]:
    history = state.get("obligation_rejection_history") or ()
    if not history:
        return {}
    return {
        "obligation_resolution": {
            "mutation_history": list(history),
        }
    }


def _record_obligation_rejection_authority(
    state: WorkflowState,
    route: Mapping[str, Any],
) -> None:
    state["obligation_rejection_history"] = tuple(
        _obligation_rejection_payload(
            _validated_obligation_rejection_history(route)
        )
    )


def _merge_compiled_obligation_rejections(
    compiled: CompiledGraph,
    route_records: Sequence[MutationRecord],
) -> CompiledGraph:
    if not route_records:
        return compiled
    records = list(compiled.mutations.records)
    for record in route_records:
        if record not in records:
            records.append(record)
    rejected_or_degraded = tuple(
        dict.fromkeys(
            (
                *compiled.mutations.rejected_or_degraded,
                *(record.capability for record in route_records),
            )
        )
    )
    status = "degraded" if compiled.status == "accepted" else compiled.status
    return replace(
        compiled,
        status=status,
        mutations=MutationLedger(
            proposed_graph=compiled.mutations.proposed_graph,
            accepted_graph=compiled.mutations.accepted_graph,
            rejected_or_degraded=rejected_or_degraded,
            records=tuple(records),
        ),
    )


def _capability_role_authority(
    *,
    request: ObligationRequest,
    reconciled: Sequence[str],
    route_selected_capabilities: Sequence[str],
    axis_role_sources: Mapping[str, Sequence[tuple[str, bool]]],
    applicable_diagnostic_tags: Sequence[str],
    registry: RuntimeContractRegistry,
) -> dict[str, dict[str, Any]]:
    sources: dict[str, list[str]] = {
        str(capability): [] for capability in reconciled
    }
    material_capabilities: set[str] = set()

    def add_source(capability: str, source: str, *, material: bool = False) -> None:
        if capability not in sources:
            return
        if source not in sources[capability]:
            sources[capability].append(source)
        if material:
            material_capabilities.add(capability)

    for capability in route_selected_capabilities:
        add_source(str(capability), "route_selected")

    for capability, role_sources in axis_role_sources.items():
        for source, material in role_sources:
            add_source(str(capability), str(source), material=bool(material))

    primary_baselines = set(request.baselines)
    for capability in reconciled:
        contract = registry.capability_inputs(str(capability))
        for window_id in contract.get("required_windows") or ():
            baseline_id = str(window_id)
            if baseline_id in primary_baselines:
                add_source(
                    str(capability),
                    f"primary_baseline:{baseline_id}",
                    material=True,
                )

    for family in request.question_families:
        contract = registry.question_family_obligation(family)
        for capability in contract["required_capabilities"]:
            add_source(
                str(capability),
                f"question_family_required:{family}",
                material=True,
            )
        for rule in contract["conditional_rules"]:
            condition = str(rule["condition"])
            if not obligation_condition_matches(condition, request, registry):
                continue
            for capability in rule["add"]:
                add_source(
                    str(capability),
                    f"question_family_conditional:{family}:{condition}",
                    material=True,
                )
        for capability in contract["independent_capabilities"]:
            add_source(
                str(capability),
                f"question_family_independent:{family}",
            )

    for tag in applicable_diagnostic_tags:
        contract = registry.diagnostic_obligation(str(tag))
        if not obligation_condition_matches(
            str(contract["condition"]), request, registry
        ):
            continue
        for capability in contract["required_capabilities"]:
            add_source(
                str(capability),
                f"diagnostic_candidate:{tag}",
            )

    authority: dict[str, dict[str, Any]] = {}
    for capability in reconciled:
        capability_id = str(capability)
        capability_sources = sources.get(capability_id) or [
            "local_required_fallback"
        ]
        authority[capability_id] = {
            "analysis_role": (
                "required"
                if capability_id in material_capabilities
                else "auxiliary"
            ),
            "sources": list(capability_sources),
        }
    return authority


def _accepted_context_window_specs(
    raw_specs: Any,
    *,
    capabilities: Sequence[str],
) -> list[dict[str, Any]]:
    accepted = {str(capability) for capability in capabilities}
    return [
        {
            "capability_id": str(spec["capability_id"]),
            "relation": str(spec["relation"]),
            "unit": str(spec["unit"]),
            "count": int(spec["count"]),
        }
        for spec in raw_specs or ()
        if isinstance(spec, Mapping)
        and str(spec.get("capability_id") or "") in accepted
    ]


def reconcile_analysis_route(
    requested: tuple[str, ...],
    route: Mapping[str, Any],
    intent: Mapping[str, Any],
    registry: RuntimeContractRegistry,
    *,
    trusted_prior_route: Mapping[str, Any] | None = None,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    carried_rejections = _validated_obligation_rejection_history(
        trusted_prior_route or {}
    )
    axis_capabilities: list[str] = []
    axis_addition_reasons: dict[str, str] = {}
    axis_role_sources: dict[str, list[tuple[str, bool]]] = {}
    for axis in intent.get("analysis_axes") or ():
        if not isinstance(axis, Mapping):
            continue
        axis_id = str(axis.get("axis_id") or "")
        role = str(axis.get("role") or "")
        explicit_focus_refs = axis.get("explicit_focus_refs")
        has_explicit_focus = bool(
            isinstance(explicit_focus_refs, Mapping)
            and any(explicit_focus_refs.values())
        )
        if role == "conditional" and not has_explicit_focus:
            continue
        for capability_id in axis.get("capability_refs") or ():
            capability = str(capability_id)
            if not capability or capability in ROUTE_BLOCKED_CAPABILITY_IDS:
                continue
            source = f"analysis_axis:{axis_id}:{role}"
            role_sources = axis_role_sources.setdefault(capability, [])
            material_axis = role in {"required", "disclosure"} or (
                role == "conditional" and has_explicit_focus
            )
            role_source = (
                source,
                material_axis,
            )
            if role_source not in role_sources:
                role_sources.append(role_source)
            if material_axis:
                axis_capabilities.append(capability)
                axis_addition_reasons.setdefault(capability, source)
    original_requested = tuple(requested)
    raw_route_requirements = route.get("analysis_requirements") or {}
    if isinstance(raw_route_requirements, Mapping):
        _validate_route_context_window_specs(
            raw_route_requirements.get("context_window_specs", ()),
            requested_capabilities=requested,
            registry=registry,
        )
    requested = registry.order_capabilities(
        (*requested, *axis_capabilities)
    )
    requested, output = _reconcile_route_input_capabilities(
        requested, route, intent, registry
    )
    route_selected_capabilities = tuple(requested)
    input_mutations = []
    original_set = set(original_requested)
    for capability in requested:
        if capability in original_set:
            continue
        contract = registry.capability_inputs(capability)
        input_mutations.append(
            {
                "action": "auto_added",
                "capability": capability,
                "reason": (
                    axis_addition_reasons[capability]
                    if capability in axis_addition_reasons
                    else "context_coverage_required"
                    if str(contract.get("source_mode") or "")
                    == "requested_context_sources"
                    else "metric_coverage_required"
                ),
            }
        )
    requirements = dict(output.get("analysis_requirements") or {})
    raw_diagnostic_value = requirements.get("diagnostic_tags") or ()
    if (
        not isinstance(raw_diagnostic_value, (list, tuple))
        or any(
            not isinstance(tag, str) or not tag
            for tag in raw_diagnostic_value
        )
        or len(raw_diagnostic_value) != len(set(raw_diagnostic_value))
    ):
        raise WorkflowFailure(
            "analysis_route_contract_invalid:diagnostic_tags",
            failure_type="llm_contract",
        )
    historically_rejected = {
        record.capability for record in carried_rejections
    }
    raw_diagnostics = tuple(
        tag
        for tag in raw_diagnostic_value
        if tag not in historically_rejected
    )
    if len(raw_diagnostics) != len(raw_diagnostic_value):
        requirements["diagnostic_tags"] = list(raw_diagnostics)
        output["analysis_requirements"] = requirements
    allowed_diagnostics = set(registry.diagnostic_obligation_ids)
    unknown_diagnostics = tuple(
        tag for tag in raw_diagnostics if tag not in allowed_diagnostics
    )
    if unknown_diagnostics:
        requirements["diagnostic_tags"] = [
            tag for tag in raw_diagnostics if tag in allowed_diagnostics
        ]
        input_mutations.extend(
            {
                "action": "rejected",
                "capability": tag,
                "reason": "unknown_diagnostic_rejected",
            }
            for tag in unknown_diagnostics
        )
        output["analysis_requirements"] = requirements
    bound_context = dict(intent)
    bound_context["analysis_requirements"] = requirements
    request = ObligationRequest.from_intent(
        question_family=str(intent.get("question_family") or ""),
        question_families=tuple(intent.get("question_families") or ()),
        target_metric=str(intent.get("target_metric") or ""),
        bound_context=bound_context,
    )
    try:
        resolution = resolve_partitioned_analysis_obligations(request, registry)
    except (KeyError, ValueError) as exc:
        error = str(exc)
        conflict_resolution = {
            "status": "conflict",
            "error": error,
            "mutations": [
                *input_mutations,
                *[
                    {
                    "action": "rejected",
                    "capability": tag,
                    "reason": "obligation_conflict",
                    }
                    for tag in request.diagnostic_tags
                    or (request.question_families[0],)
                ],
            ],
        }
        if carried_rejections:
            conflict_resolution["mutation_history"] = (
                _obligation_rejection_payload(carried_rejections)
            )
            conflict_resolution["rejected_diagnostic_tags"] = list(
                dict.fromkeys(
                    record.capability for record in carried_rejections
                )
            )
        output["obligation_resolution"] = conflict_resolution
        return requested, output

    rejected_diagnostic_mutations = [
        dict(mutation)
        for mutation in resolution.mutations
        if mutation.get("action") == "rejected"
    ]
    if rejected_diagnostic_mutations:
        requirements["diagnostic_tags"] = list(
            resolution.applicable_diagnostic_tags
        )
        output["analysis_requirements"] = requirements
        input_mutations.extend(rejected_diagnostic_mutations)

    obligations = (
        *resolution.required_capabilities,
        *resolution.conditional_capabilities,
        *resolution.independent_capabilities,
    )
    reconciled = tuple(dict.fromkeys((*requested, *obligations)))
    requested_set = set(requested)
    capability_roles = _capability_role_authority(
        request=request,
        reconciled=reconciled,
        route_selected_capabilities=route_selected_capabilities,
        axis_role_sources=axis_role_sources,
        applicable_diagnostic_tags=resolution.applicable_diagnostic_tags,
        registry=registry,
    )
    requirements["context_window_specs"] = _accepted_context_window_specs(
        requirements.get("context_window_specs") or (),
        capabilities=reconciled,
    )
    output["analysis_requirements"] = requirements
    obligation_mutations = [
        {
            "action": "auto_added",
            "capability": capability,
            "reason": (
                "obligation_independent"
                if capability in resolution.independent_capabilities
                else "obligation_auxiliary"
                if capability_roles[capability]["analysis_role"] == "auxiliary"
                else "obligation_conditional"
                if capability in resolution.conditional_capabilities
                else "obligation_required"
            ),
        }
        for capability in obligations
        if capability not in requested_set
    ]
    capability_datasets = capability_dataset_requirements(
        reconciled,
        request.target_metrics,
        registry,
    )
    raw_carried_datasets = requirements.get("dataset_requirements") or ()
    if isinstance(raw_carried_datasets, str):
        carried_datasets = [raw_carried_datasets]
    elif isinstance(raw_carried_datasets, Sequence) and not isinstance(
        raw_carried_datasets, (str, bytes)
    ):
        carried_datasets = list(raw_carried_datasets)
    else:
        raise ValueError("analysis_requirements_dataset_requirements_invalid")
    carried_datasets.extend(
        dataset_id
        for capability_id in reconciled
        for dataset_id in capability_datasets.get(capability_id, ())
    )
    if carried_datasets:
        requirements["dataset_requirements"] = list(
            dict.fromkeys(str(item) for item in carried_datasets if item)
        )
        output["analysis_requirements"] = requirements
    rejection_history = _merge_obligation_rejection_history(
        carried_rejections,
        input_mutations,
    )
    obligation_resolution = {
        "status": "resolved",
        "required_capabilities": [
            capability
            for capability in reconciled
            if capability_roles[capability]["analysis_role"] == "required"
        ],
        "conditional_capabilities": list(resolution.conditional_capabilities),
        "independent_capabilities": list(resolution.independent_capabilities),
        "auxiliary_capabilities": [
            capability
            for capability in reconciled
            if capability_roles[capability]["analysis_role"] == "auxiliary"
        ],
        "capability_roles": capability_roles,
        "minimum_publishable_evidence": list(
            resolution.minimum_publishable_evidence
        ),
        "applicable_diagnostic_tags": list(
            resolution.applicable_diagnostic_tags
        ),
        "rejected_diagnostic_tags": list(
            dict.fromkeys(
                (
                    *resolution.rejected_diagnostic_tags,
                    *(record.capability for record in rejection_history),
                )
            )
        ),
        "capability_dataset_requirements": {
            capability_id: list(dataset_ids)
            for capability_id, dataset_ids in capability_datasets.items()
        },
        "mutations": [*input_mutations, *obligation_mutations],
    }
    if rejection_history:
        obligation_resolution["mutation_history"] = (
            _obligation_rejection_payload(rejection_history)
        )
    output["obligation_resolution"] = obligation_resolution
    return reconciled, output


def _consume_obligation_route_conflict(
    state: WorkflowState,
    route: Mapping[str, Any],
) -> None:
    resolution = route.get("obligation_resolution") or {}
    if not isinstance(resolution, Mapping) or resolution.get("status") != "conflict":
        return
    conflicts = tuple(state.get("route_material_conflicts") or ())
    state["route_material_conflicts"] = tuple(
        dict.fromkeys((*conflicts, "analysis_obligations"))
    )
    state["boundary_decision"] = {
        "boundary_status": "needs_question",
        "recommended_assumption": "保留已确认的业务意图，并选择合同支持的诊断路线继续。",
        "clarification_questions": [],
        "decision_summary": "诊断要求与问题类型合同冲突，需要用户确认分析路线。",
    }


def _apply_query_gap_action_to_route(
    requested: tuple[str, ...],
    route: Mapping[str, Any],
    action: Mapping[str, Any],
    registry: RuntimeContractRegistry,
    *,
    preserve_narrative: bool = False,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    action_kind = str(action.get("action_kind") or "")
    if action_kind == "choose_supported_window":
        output = dict(route)
        requirements = dict(output.get("analysis_requirements") or {})
        supported = {
            "previous_day",
            "rolling_7_day_baseline",
            "same_weekday_last_week",
        }
        requirements["baselines"] = [
            baseline
            for baseline in _canonical_baselines(
                requirements.get("baselines") or ()
            )
            if baseline in supported
        ]
        output["analysis_requirements"] = requirements
        if not preserve_narrative:
            output["decision_summary"] = "已采用合同支持的业务时间窗口。"
        return requested, output
    if action_kind == "choose_supported_claim_intent":
        output = dict(route)
        requirements = dict(output.get("analysis_requirements") or {})
        supported_claims: set[str] = set()
        for capability_id in requested:
            try:
                contract = registry.capability_inputs(capability_id)
            except KeyError:
                continue
            supported_claims.update(
                str(claim_type)
                for claim_type in contract.get("supported_claim_types", ())
            )
        requirements["claim_types"] = [
            str(claim_type)
            for claim_type in requirements.get("claim_types") or ()
            if str(claim_type) in supported_claims
        ]
        output["analysis_requirements"] = requirements
        if not preserve_narrative:
            output["decision_summary"] = "已采用合同支持的声明强度继续。"
        return requested, output
    if action_kind not in {
        "omit_unavailable_context",
        "continue_with_boundary_only",
    }:
        return requested, dict(route)
    affected = {
        str(capability)
        for capability in action.get("affected_capabilities") or ()
        if str(capability)
    }
    remaining = tuple(
        capability for capability in requested if capability not in affected
    )
    if not affected:
        return requested, dict(route)
    output = dict(route)
    output["requested_nodes"] = list(remaining)
    if not preserve_narrative:
        output["route_summary"] = (
            "按用户选择继续执行可验证的主指标分析，并在结论中保留缺失背景证据的限制。"
        )
        output["decision_summary"] = "已移除当前不可用的背景证据路径。"
        output["expected_evidence"] = [
            f"{label}：产出主指标判断及证据限制。"
            for label in _capability_labels(remaining)
        ]
    return remaining, output


def _accept_analysis_route(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "accept_analysis_route")
    intent = state["intent"]
    request = state.get("request", {})
    registry = RuntimeContractRegistry.from_path(
        CANONICAL_RUNTIME_BINDINGS_PATH
    )
    accepted_route = state.get("analysis_route") or {}
    requested = tuple(accepted_route.get("requested_nodes") or ())
    _validate_final_analysis_route_mapping(
        accepted_route,
        requested=requested,
        known_capability_ids=frozenset(registry.capability_ids),
        allow_empty=not requested,
    )
    analysis_runtime = request.get("analysis_runtime")
    analysis_outcome = None
    if analysis_runtime is not None:
        runtime_request = _analysis_runtime_request(state)
        analysis_outcome = analysis_runtime.compile(runtime_request)
        _record_execution_material(
            state,
            runtime_request,
            analysis_runtime,
            analysis_outcome.analysis_contract,
            analysis_outcome.query_contracts,
            analysis_outcome.capability_plans,
        )
        state["analysis_compile_outcome"] = analysis_outcome
        state["request"]["analysis_compile_outcome"] = analysis_outcome
        state["request"]["analysis_contract"] = analysis_outcome.analysis_contract
        state["request"]["query_contracts"] = analysis_outcome.query_contracts
        state["request"]["capability_execution_plans"] = analysis_outcome.capability_plans
    compiled = _typed_clarification_compiled_graph(analysis_outcome, intent)
    if compiled is None:
        compiled = compile_graph(
            question_family=intent["question_family"],
            target_metric=intent["target_metric"],
            pattern_family=intent["pattern_family"],
            requested_nodes=intent["requested_nodes"],
            question_families=intent.get("question_families", ()),
            question_text=str(state["request"].get("question") or ""),
            bound_context=_compiler_bound_context(state),
            prior_analysis_assets=tuple(state["request"].get("prior_analysis_assets") or ()),
        )
    compiled = _merge_compiled_obligation_rejections(
        compiled,
        _validated_obligation_rejection_history(
            _trusted_obligation_rejection_route(state)
        ),
    )
    state["compiled_graph"] = compiled
    accepted_choice = dict(request.get("accepted_degradation_choice") or {})
    if accepted_choice:
        state["clarification_outcome"] = {
            **dict(state.get("clarification_outcome") or {}),
            "accepted_degradation_choice": accepted_choice,
        }
        compiled = replace(
            compiled,
            runtime_plan={
                **dict(compiled.runtime_plan),
                "graph_metadata": {
                    "accepted_assumptions": [accepted_choice],
                },
            },
        )
        state["compiled_graph"] = compiled
    if "formula_decompose" in compiled.mutations.accepted_graph:
        framework = _formula_candidate_framework(state)
        compiled = replace(
            compiled,
            runtime_plan={
                **dict(compiled.runtime_plan),
                "formula_candidate_framework": framework,
            },
        )
        state["compiled_graph"] = compiled
    state["request"]["compiler_runtime_plan"] = compiled.runtime_plan
    _refresh_contract_gap_diagnostics(state)
    return state


def _typed_clarification_compiled_graph(
    outcome: Any,
    intent: Mapping[str, Any],
) -> CompiledGraph | None:
    if outcome is None:
        return None
    needs_clarification = analysis_outcome_requires_route_clarification(outcome)
    accepted_graph = tuple(
        dict.fromkeys(str(item) for item in intent.get("requested_nodes", ()) if item)
    )
    if not accepted_graph:
        return None
    if not needs_clarification:
        plans = {plan.capability_id: plan for plan in outcome.capability_plans}
        for capability in accepted_graph:
            plan = plans.get(capability)
            if plan is None:
                return None
            for slot in plan.required_input_slots:
                required = (
                    bool(slot.get("required", True))
                    if isinstance(slot, Mapping)
                    else bool(slot.required)
                )
                query_refs = (
                    tuple(slot.get("query_contract_refs") or ())
                    if isinstance(slot, Mapping)
                    else slot.query_contract_refs
                )
                validation_refs = (
                    tuple(slot.get("validation_query_contract_refs") or ())
                    if isinstance(slot, Mapping)
                    else slot.validation_query_contract_refs
                )
                if required and not query_refs and not validation_refs:
                    return None
        if any(
            capability in gap.affected_capabilities
            for gap in outcome.analysis_contract.contract_gaps
            for capability in accepted_graph
        ):
            return None
    runtime_plan = {
        "analysis_contract": outcome.analysis_contract.to_dict(),
        "query_contracts": [item.to_dict() for item in outcome.query_contracts],
        "capability_execution_plans": [
            asdict(item)
            if is_dataclass(item)
            else dict(item)
            if isinstance(item, Mapping)
            else dict(vars(item))
            for item in outcome.capability_plans
        ],
    }
    return CompiledGraph(
        status="needs_clarification" if needs_clarification else "accepted",
        accepted_nodes=tuple(
            GraphNode(
                node_id=capability,
                capability=capability,
                status="accepted",
                target_claim=str(intent.get("target_claim") or ""),
            )
            for capability in accepted_graph
        ),
        mutations=MutationLedger(
            proposed_graph=accepted_graph,
            accepted_graph=accepted_graph,
            rejected_or_degraded=(),
            records=(),
        ),
        runtime_plan=runtime_plan,
        analysis_contract=runtime_plan["analysis_contract"],
        query_contracts=tuple(runtime_plan["query_contracts"]),
        capability_execution_plans=tuple(
            runtime_plan["capability_execution_plans"]
        ),
    )


def _record_execution_material(
    state: WorkflowState,
    runtime_request: AnalysisRuntimeRequest,
    analysis_runtime: Any,
    analysis_contract: Any,
    query_contracts: Iterable[Any],
    capability_execution_plans: Iterable[Any],
) -> None:
    registry = analysis_runtime.registry
    state["execution_material"] = build_execution_material(
        proposal=runtime_request.proposal,
        accepted_graph=runtime_request.accepted_graph,
        as_of=runtime_request.as_of,
        run_mode=runtime_request.run_mode,
        runtime_contract_version=registry.contract_version,
        runtime_registry_digest=registry.source_payload_digest,
        analysis_contract=analysis_contract,
        query_contracts=query_contracts,
        capability_execution_plans=capability_execution_plans,
    )


def _runtime_capability_role_authority(
    route: Mapping[str, Any],
    accepted_graph: Sequence[str],
) -> dict[str, dict[str, Any]]:
    capability_ids = tuple(dict.fromkeys(str(item) for item in accepted_graph if item))
    resolution = route.get("obligation_resolution")
    raw_roles = (
        resolution.get("capability_roles")
        if isinstance(resolution, Mapping)
        else None
    )
    if raw_roles is None:
        return {
            capability_id: {
                "analysis_role": "required",
                "sources": ["runtime_fail_closed"],
            }
            for capability_id in capability_ids
        }
    if not isinstance(raw_roles, Mapping) or set(raw_roles) != set(capability_ids):
        raise WorkflowFailure(
            "analysis_route_capability_roles_invalid:coverage",
            failure_type="contract",
        )
    roles: dict[str, dict[str, Any]] = {}
    for capability_id in capability_ids:
        raw = raw_roles.get(capability_id)
        if not isinstance(raw, Mapping) or set(raw) != {
            "analysis_role",
            "sources",
        }:
            raise WorkflowFailure(
                "analysis_route_capability_roles_invalid:shape",
                failure_type="contract",
            )
        analysis_role = raw.get("analysis_role")
        sources = raw.get("sources")
        if (
            analysis_role not in {"required", "auxiliary"}
            or not isinstance(sources, (list, tuple))
            or not sources
            or any(
                not isinstance(source, str)
                or not source
                or source != source.strip()
                for source in sources
            )
            or len(sources) != len(set(sources))
        ):
            raise WorkflowFailure(
                "analysis_route_capability_roles_invalid:value",
                failure_type="contract",
            )
        roles[capability_id] = {
            "analysis_role": str(analysis_role),
            "sources": list(sources),
        }
    declared_auxiliary = (
        resolution.get("auxiliary_capabilities")
        if isinstance(resolution, Mapping)
        else None
    )
    if declared_auxiliary is not None and (
        not isinstance(declared_auxiliary, (list, tuple))
        or tuple(declared_auxiliary)
        != tuple(
            capability_id
            for capability_id in capability_ids
            if roles[capability_id]["analysis_role"] == "auxiliary"
        )
    ):
        raise WorkflowFailure(
            "analysis_route_capability_roles_invalid:auxiliary_projection",
            failure_type="contract",
        )
    return roles


def _analysis_runtime_request(state: WorkflowState) -> AnalysisRuntimeRequest:
    request = state.get("request") or {}
    route = state.get("analysis_route") or {}
    intent = state.get("intent") or {}
    if isinstance(intent.get("baseline_binding"), Mapping) and (
        _material_baseline_clarification_needed(state)
    ):
        raise WorkflowFailure(
            "analysis_runtime_material_unbound:baseline",
            failure_type="contract",
        )

    requirements = route.get("analysis_requirements")
    proposal = dict(requirements) if isinstance(requirements, Mapping) else {}
    if "claim_types" in proposal:
        proposal["claim_types"] = _claim_intent_values(
            proposal.get("claim_types")
        )
    if proposal.get("context_sources") and not proposal.get(
        "requested_context_sources"
    ):
        proposal["requested_context_sources"] = proposal["context_sources"]

    proposal.setdefault(
        "question_families",
        tuple(
            intent.get("question_families")
            or (intent.get("question_family"),)
        ),
    )
    proposal.setdefault("target_metrics", (intent.get("target_metric"),))
    proposal.setdefault(
        "component_ids", tuple(intent.get("component_ids") or ())
    )
    proposal.setdefault(
        "association_metric_ids",
        tuple(intent.get("association_metric_ids") or ()),
    )
    proposal.setdefault(
        "dimension_ids", tuple(intent.get("dimension_ids") or ())
    )
    proposal.setdefault(
        "baselines",
        tuple(
            intent.get("baseline_candidates")
            or intent.get("baselines")
            or ()
        ),
    )
    proposal.setdefault(
        "claim_types", tuple(intent.get("publishable_claim_types") or ())
    )
    proposal.setdefault(
        "required_outcomes", tuple(intent.get("required_outcomes") or ())
    )
    proposal.setdefault(
        "analysis_axis_ids", tuple(intent.get("analysis_axis_ids") or ())
    )
    proposal.setdefault(
        "scope", intent.get("scope") or {"type": "full_sample"}
    )
    proposal.setdefault(
        "target_semantic", intent.get("target_semantic") or "yesterday"
    )

    choice = request.get("clarification_choice") or {}
    if isinstance(choice, Mapping):
        for key in (
            "target_semantic",
            "baselines",
            "dimension_ids",
            "claim_types",
            "scope",
        ):
            if choice.get(key) not in (None, "", (), [], {}):
                proposal[key] = choice[key]
        if choice.get("baseline_candidates") not in (None, "", (), [], {}):
            proposal["baselines"] = choice["baseline_candidates"]
        if choice.get("target_window"):
            proposal["target_semantic"] = str(choice["target_window"])

    accepted_choice = request.get("accepted_degradation_choice") or {}
    if isinstance(accepted_choice, Mapping) and accepted_choice:
        proposal["accepted_degradation_choice"] = dict(accepted_choice)

    proposal["baselines"] = _canonical_baselines(
        proposal.get("baselines") or ()
    )
    proposal["context_window_specs"] = _canonical_context_window_specs(
        proposal.get("context_window_specs") or ()
    )
    analysis_context = request.get("analysis_context") or {}
    if not isinstance(analysis_context, Mapping):
        raise WorkflowFailure(
            "analysis_context_shape_invalid",
            failure_type="contract",
        )
    fixed_window_bounds = _fixed_window_bounds(analysis_context)
    if fixed_window_bounds:
        proposal["fixed_window_bounds"] = fixed_window_bounds
    as_of = analysis_context.get("as_of") or datetime.now(timezone.utc)

    accepted_graph = tuple(
        state.get("analysis_route", {}).get("requested_nodes") or ()
    )
    proposal["capability_roles"] = _runtime_capability_role_authority(
        route,
        accepted_graph,
    )
    run_mode = str(request.get("run_mode") or "production")

    # The runtime compiler still names these slots after their executable
    # contract role. This projection is local and never re-enters LLM intent.
    proposal["requested_components"] = tuple(
        proposal.pop("component_ids", ()) or ()
    )
    proposal["association_metrics"] = tuple(
        proposal.pop("association_metric_ids", ()) or ()
    )
    proposal["requested_dimensions"] = tuple(
        proposal.pop("dimension_ids", ()) or ()
    )
    proposal["claim_intents"] = _claim_intent_values(
        proposal.pop("claim_types", ())
    )
    proposal["required_claim_intents"] = tuple(
        intent.get("required_claim_types") or ()
    )
    proposal["candidate_claim_intents"] = tuple(
        intent.get("auxiliary_claim_types") or ()
    )
    return AnalysisRuntimeRequest.create(
        run_id=str(state.get("run_id") or request.get("run_id") or ""),
        topic_id=str(request.get("topic_id") or ""),
        proposal=proposal,
        accepted_graph=accepted_graph,
        as_of=as_of,
        reuse_candidates=tuple(request.get("reuse_candidates") or ()),
        attempted_signatures=tuple(
            request.get("attempted_query_signatures") or ()
        ),
        run_mode=run_mode,
    )


def _fixed_window_bounds(
    analysis_context: Mapping[str, Any],
) -> dict[str, tuple[str, str]]:
    target = str(analysis_context.get("target_date") or "")
    previous = str(analysis_context.get("previous_day") or "")
    rolling_start = str(analysis_context.get("rolling_7_day_start") or "")
    rolling_end = str(analysis_context.get("rolling_7_day_end") or "")
    same_weekday = str(analysis_context.get("same_weekday_last_week") or "")
    pattern_start = str(analysis_context.get("pattern_history_start") or "")
    anomaly_start = str(analysis_context.get("anomaly_history_start") or "")
    bounds = {
        "target_day": (target, target),
        "previous_day": (previous, previous),
        "rolling_7_day_baseline": (rolling_start, rolling_end),
        "same_weekday_last_week": (same_weekday, same_weekday),
        "pattern_history": (pattern_start, target),
        "anomaly_history": (anomaly_start, previous),
    }
    return {
        window_id: value
        for window_id, value in bounds.items()
        if all(value)
    }


def _canonical_baselines(values: Sequence[Any]) -> tuple[str, ...]:
    items = (
        tuple(values)
        if isinstance(values, Sequence)
        and not isinstance(values, (str, bytes, bytearray))
        else (values,)
    )
    output: list[str] = []
    for value in items:
        try:
            canonical = canonical_baseline_ids(value)
        except BaselineSemanticError:
            canonical = (str(value),) if str(value) else ()
        for item in canonical:
            if item not in output:
                output.append(item)
    return tuple(output)


def _canonical_context_window_specs(values: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(values, (list, tuple)):
        raise WorkflowFailure(
            "analysis_route_contract_invalid:analysis_requirements:"
            "context_window_specs",
            failure_type="contract",
        )
    if any(not isinstance(value, Mapping) for value in values):
        raise WorkflowFailure(
            "analysis_route_contract_invalid:analysis_requirements:"
            "context_window_specs",
            failure_type="contract",
        )
    return tuple(
        {
            "capability_id": str(value["capability_id"]),
            "relation": str(value["relation"]),
            "unit": str(value["unit"]),
            "count": int(value["count"]),
        }
        for value in values
    )


def _repair_analysis_route(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "repair_analysis_route")
    state["repair_attempts"] = state.get("repair_attempts", 0) + 1
    current_route = dict(state["analysis_route"])
    registry = RuntimeContractRegistry.from_path(
        CANONICAL_RUNTIME_BINDINGS_PATH
    )
    capability_cards = _route_capability_cards()
    repair_required_capability_ids = tuple(
        dict.fromkeys(
            (
                *_requested_node_ids(current_route.get("requested_nodes")),
                *_deterministic_required_route_capabilities(state, registry),
            )
        )
    )
    allowed_repair_capability_ids = frozenset(
        {
            str(card.get("capability_id") or "")
            for card in capability_cards
            if str(card.get("capability_id") or "")
        }
        | set(repair_required_capability_ids)
    )
    output = _invoke_llm(
        state,
        "route_repair",
        {
            "intent": state["intent"],
            "analysis_route": state["analysis_route"],
            "known_capabilities": capability_cards,
            "allowed_capability_ids": sorted(
                allowed_repair_capability_ids
            ),
            "required_capability_ids": list(
                repair_required_capability_ids
            ),
            "compiler_feedback": to_jsonable(
                state.get("request", {}).get("analysis_repair_feedback")
                or state["compiled_graph"].mutations.records
            ),
            "repair_attempt": state["repair_attempts"],
        },
        output_validator=lambda value: _validate_route_repair_provider_output(
            _project_route_repair_provider_output(value),
            current_route,
            allowed_capability_ids=allowed_repair_capability_ids,
            state=state,
            registry=registry,
        ),
    )
    output = _project_route_repair_provider_output(output)
    if _route_repair_has_material_conflict(output, current_route):
        raise WorkflowFailure(
            "analysis_route_repair_material_conflict:analysis_requirements",
            failure_type="contract",
        )
    try:
        _validate_route_repair_provider_output(
            output,
            current_route,
            allowed_capability_ids=allowed_repair_capability_ids,
            state=state,
            registry=registry,
        )
    except LLMOutputError as exc:
        raise WorkflowFailure(
            str(exc),
            failure_type="contract",
        ) from exc
    repaired_route = {**current_route, **output}
    requested = _requested_node_ids(
        output.get("requested_nodes"),
        excluded=ROUTE_BLOCKED_CAPABILITY_IDS,
    )
    if not requested:
        requested = tuple(current_route.get("requested_nodes") or ())
    requested, output = reconcile_analysis_route(
        requested,
        repaired_route,
        state["intent"],
        registry,
        trusted_prior_route=_trusted_obligation_rejection_route(state),
    )
    _consume_obligation_route_conflict(state, output)
    accepted_choice = dict(
        state.get("current_query_gap_choice") or {}
    )
    requested, output = _apply_query_gap_action_to_route(
        requested,
        output,
        accepted_choice,
        registry,
        preserve_narrative=True,
    )
    output = _finalize_production_analysis_route_narrative(
        state,
        route=output,
        requested=requested,
        registry=registry,
    )
    _validate_final_analysis_route_mapping(
        {**output, "requested_nodes": requested},
        requested=requested,
        known_capability_ids=frozenset(registry.capability_ids),
        allow_empty=not requested,
    )
    _infer_question_families_from_requested_nodes(state["intent"], requested)
    state["analysis_route"] = {**state["analysis_route"], **output, "requested_nodes": requested}
    _record_obligation_rejection_authority(state, state["analysis_route"])
    state["intent"]["requested_nodes"] = requested
    return state


def _project_route_repair_provider_output(
    output: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        key: output[key]
        for key in _ROUTE_REPAIR_PROVIDER_FIELDS
        if key in output
    }


def _validate_route_repair_provider_output(
    output: Mapping[str, Any],
    current_route: Mapping[str, Any],
    *,
    allowed_capability_ids: frozenset[str],
    state: WorkflowState,
    registry: RuntimeContractRegistry,
) -> None:
    if _route_repair_has_material_conflict(output, current_route):
        raise LLMOutputError(
            "analysis_route_repair_material_conflict:analysis_requirements"
        )
    proposed_nodes = output.get("requested_nodes")
    if proposed_nodes is not None and (
        not isinstance(proposed_nodes, (list, tuple))
        or any(
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or item not in allowed_capability_ids
            for item in proposed_nodes
        )
        or len(proposed_nodes) != len(set(proposed_nodes))
    ):
        raise LLMOutputError(
            "analysis_route_repair_contract_invalid:requested_nodes"
        )
    requested = _requested_node_ids(proposed_nodes)
    if not requested:
        requested = _requested_node_ids(current_route.get("requested_nodes"))
    try:
        reconcile_analysis_route(
            requested,
            {**dict(current_route), **dict(output)},
            state.get("intent") or {},
            registry,
            trusted_prior_route=_trusted_obligation_rejection_route(state),
        )
    except (KeyError, TypeError, ValueError, WorkflowFailure) as exc:
        raise LLMOutputError(_exception_reason(exc)) from exc


def _route_repair_has_material_conflict(
    output: Mapping[str, Any],
    current_route: Mapping[str, Any],
) -> bool:
    return (
        "analysis_requirements" in output
        and "analysis_requirements" in current_route
        and canonical_value(output["analysis_requirements"])
        != canonical_value(current_route["analysis_requirements"])
    )


def _inspect_schema(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "inspect_schema")
    sql_text = state["request"].get(
        "sql_text",
        "SELECT month, phase, sum(amount) AS amount "
        "FROM paid_order_detail GROUP BY month, phase",
    )
    rows = list(state["request"].get("rows") or [])
    fields = (
        tuple(rows[0].keys())
        if rows
        else tuple(state["request"].get("required_fields", ("month", "phase", "amount")))
    )
    state["sql_text"] = sql_text
    state["schema"] = {
        "table": "phase4_aggregate_result",
        "fields": fields,
        "grain": state["intent"]["pattern_family"],
        "pattern_params": state["intent"].get("pattern_params", {}),
    }
    return state


def _validate_runtime_binding(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "validate_runtime_binding")
    if state.get("request", {}).get("analysis_runtime") is not None:
        state["sql_hash"] = ""
        state["validator_results"] = [
            {
                "validator": "runtime_binding",
                "ok": True,
                "reason": "analysis_runtime_bound",
            },
            {
                "validator": "sensitive_output_policy",
                "ok": True,
                "reason": "aggregate_only",
            },
        ]
        return state
    sql_result = validate_select_only(state["sql_text"], aggregate=True)
    state["sql_hash"] = sql_result.query_hash
    validator_results = [
        {
            "validator": "sql_safety",
            "ok": sql_result.ok,
            "reason": sql_result.reason,
            "sql_hash": sql_result.query_hash,
        },
        {
            "validator": "runtime_binding",
            "ok": True,
            "reason": "phase4_draft_binding",
        },
        {
            "validator": "sensitive_output_policy",
            "ok": True,
            "reason": "aggregate_only",
        },
    ]
    state["validator_results"] = validator_results
    return state


def _fetch_runtime_rows(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "fetch_runtime_rows")
    analysis_runtime = state.get("request", {}).get("analysis_runtime")
    if analysis_runtime is not None:
        runtime_request = _analysis_runtime_request(state)
        result = analysis_runtime.execute(runtime_request)
        _record_execution_material(
            state,
            runtime_request,
            analysis_runtime,
            result.analysis_contract,
            result.query_contracts,
            result.capability_plans,
        )
        state["analysis_runtime_result"] = result
        payload = result.to_workflow_payload()
        state["request"].update(payload)
        state["request"]["reuse_decisions"] = list(
            payload.get("reuse_decisions") or ()
        )
        state["request"]["runtime_rows_source"] = "analysis_runtime"
        state["request"]["rows"] = tuple(
            row
            for rows in payload["runtime_rows_by_intent"].values()
            for row in rows
        )
        state["request"]["result_refs"] = tuple(
            dict.fromkeys(
                ref
                for refs in payload["result_refs_by_intent"].values()
                for ref in refs
            )
        )
        state["request"]["bound_capability_inputs"] = dict(
            result.bound_capability_inputs
        )
        state["query_repair_decisions"] = tuple(
            asdict(item) for item in result.repair_decisions
        )
        query_results = tuple(result.query_results)
        results_by_query_ref = {
            item.query_contract_ref: item for item in query_results
        }
        required_query_refs = {
            query_ref
            for plan in result.capability_plans
            if result.capability_roles.get(plan.capability_id, "required")
            != "auxiliary"
            for slot in plan.required_input_slots
            for query_ref in (
                *slot.query_contract_refs,
                *slot.validation_query_contract_refs,
            )
        }
        succeeded = bool(required_query_refs) and all(
            query_ref in results_by_query_ref
            and results_by_query_ref[query_ref].execution_status == "succeeded"
            for query_ref in required_query_refs
        )
        auxiliary_unavailable = any(
            item.execution_status != "succeeded"
            and item.query_contract_ref not in required_query_refs
            for item in query_results
        )
        state.setdefault("validator_results", []).append(
            {
                "validator": "clickhouse_runtime",
                "ok": succeeded,
                "reason": (
                    "primary_rows_loaded_with_auxiliary_limits"
                    if succeeded and auxiliary_unavailable
                    else "provider_rows_loaded"
                    if succeeded
                    else "provider_rows_unavailable"
                ),
                "result_refs": [
                    item.result_ref for item in query_results if item.result_ref
                ],
            }
        )
        rows = tuple(state["request"]["rows"])
        fields = tuple(rows[0]) if rows else ()
        state["schema"] = {
            "fields": fields,
            "row_source": "analysis_runtime",
            "grain": tuple(
                dict.fromkeys(
                    field
                    for contract in result.query_contracts
                    for field in contract.result_shape.grain
                )
            ),
        }
        return state
    reused_asset_input = _reused_dimension_scan_input(state)
    provider = _runtime_row_provider(state["request"])
    if provider is None:
        if reused_asset_input:
            _apply_reused_dimension_scan_input(state, reused_asset_input)
            state.setdefault("validator_results", []).append(
                {
                    "validator": "analysis_asset_runtime",
                    "ok": True,
                    "reason": "prior_dimension_scan_rows_loaded",
                    "query_ref": reused_asset_input.get("query_ref", ""),
                }
            )
        return state
    if not provider.configured():
        if reused_asset_input:
            _apply_reused_dimension_scan_input(state, reused_asset_input)
            state.setdefault("validator_results", []).append(
                {
                    "validator": "analysis_asset_runtime",
                    "ok": True,
                    "reason": "prior_dimension_scan_rows_loaded",
                    "query_ref": reused_asset_input.get("query_ref", ""),
                }
            )
            return state
        state.setdefault("validator_results", []).append(
            {
                "validator": "clickhouse_runtime",
                "ok": False,
                "reason": provider.binding_reason(),
            }
        )
        return state

    plan = provider.plan(
        state["request"],
        state["intent"],
        tuple(state["compiled_graph"].mutations.accepted_graph),
    )
    query_intent = _row_query_intent(plan.query_id, plan.reason)
    state["row_query_plan"] = {
        "sql_text": plan.sql_text,
        "query_id": plan.query_id,
        "required_fields": list(plan.required_fields),
        "dimension_keys": list(plan.dimension_keys),
        "query_intent": query_intent,
        "reason": plan.reason,
        "query_plans": [
            {
                "sql_text": item.sql_text,
                "query_id": item.query_id,
                "query_intent": item.intent,
                "required_fields": list(item.required_fields),
                "dimension_keys": list(item.dimension_keys),
                "reason": item.reason,
                "claim_use": item.claim_use,
            }
            for item in plan.query_plans
        ],
        "compiler_runtime_plan": to_jsonable(
            state["request"].get("compiler_runtime_plan", {})
        ),
    }
    if plan.sql_text:
        state["sql_text"] = plan.sql_text
    state["request"]["required_fields"] = tuple(plan.required_fields)

    result = provider.fetch(plan)
    if not result.ok:
        state.setdefault("validator_results", []).append(
            {
                "validator": "clickhouse_query",
                "ok": False,
                "reason": result.reason,
                "sql_hash": result.query_hash,
                "query_id": result.query_id or plan.query_id,
            }
        )
        return state

    rows = [dict(row) for row in result.rows]
    rows_by_intent = {
        str(intent): [dict(row) for row in intent_rows]
        for intent, intent_rows in result.rows_by_intent.items()
    }
    result_refs_by_intent = {
        str(intent): list(refs)
        for intent, refs in result.result_refs_by_intent.items()
    }
    if rows and query_intent not in rows_by_intent:
        rows_by_intent[query_intent] = rows
    if result.result_refs and query_intent not in result_refs_by_intent:
        result_refs_by_intent[query_intent] = list(result.result_refs)
    state["request"]["rows"] = rows
    state["request"]["runtime_rows_source"] = "clickhouse"
    state["request"]["runtime_rows_by_intent"] = rows_by_intent
    state["request"]["result_refs_by_intent"] = result_refs_by_intent
    state["request"]["query_results"] = to_jsonable(result.query_results)
    state["request"]["joint_dimension_keys"] = tuple(plan.dimension_keys)
    state["request"]["result_refs"] = tuple(result.result_refs)
    state["row_query_plan"]["query_hash"] = result.query_hash
    state["row_query_plan"]["result_refs"] = list(result.result_refs)
    state["row_query_plan"]["rows"] = rows
    state["row_query_plan"]["rows_by_intent"] = rows_by_intent
    state["row_query_plan"]["result_refs_by_intent"] = result_refs_by_intent
    state["row_query_plan"]["query_results"] = to_jsonable(result.query_results)
    if result.query_hash:
        state["sql_hash"] = result.query_hash
    if reused_asset_input:
        _apply_reused_dimension_scan_input(
            state,
            reused_asset_input,
            additional_result_refs=result.result_refs,
        )
    if state.get("schema") is not None:
        effective_rows = state["request"].get("rows") or rows
        fields = tuple(effective_rows[0].keys()) if effective_rows else tuple(plan.required_fields)
        state["schema"] = {
            **state["schema"],
            "fields": fields,
            "row_source": state["request"].get("runtime_rows_source", "clickhouse"),
            "query_id": result.query_id or plan.query_id,
        }
    state.setdefault("validator_results", []).append(
        {
            "validator": "clickhouse_runtime",
            "ok": True,
            "reason": "provider_rows_loaded",
            "sql_hash": result.query_hash,
            "query_id": result.query_id or plan.query_id,
        }
    )
    return state


def _validate_query_completeness(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "validate_query_completeness")
    result = state.get("analysis_runtime_result")
    if result is None:
        return state
    for report in result.completeness_reports:
        state.setdefault("validator_results", []).append(
            {
                "validator": "query_completeness",
                "ok": report.analysis_readiness in {"ready", "degraded"},
                "reason": (
                    "complete"
                    if report.analysis_readiness == "ready"
                    else ",".join(report.failure_reasons)
                ),
                "report_ref": report.report_ref,
                "completeness_status": report.completeness_status,
                "analysis_readiness": report.analysis_readiness,
            }
        )
    if not result.query_results:
        state.setdefault("validator_results", []).append(
            {
                "validator": "query_completeness",
                "ok": False,
                "reason": "typed_query_results_unavailable",
            }
        )
    return state


def _decide_query_repair(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "decide_query_repair")
    result = state.get("analysis_runtime_result")
    state["query_repair_decisions"] = tuple(
        asdict(item) for item in (result.repair_decisions if result is not None else ())
    )
    return state


def _route_after_query_repair(state: WorkflowState) -> str:
    result = state.get("analysis_runtime_result")
    if result is None:
        return "block" if _route_after_runtime_rows(state) == "block" else "ready"
    terminal_window_unavailable = any(
        str(decision.get("action") or "") == "block"
        and str(decision.get("reason") or "") == "window_coverage_failure"
        for decision in state.get("query_repair_decisions") or ()
        if isinstance(decision, Mapping)
    )
    if terminal_window_unavailable:
        typed_gaps = tuple(
            dict(item)
            for item in getattr(result, "typed_gaps", ())
            if isinstance(item, Mapping)
        )
        if _has_authority_ready_independent_capability(result, typed_gaps):
            state["accepted_degraded_query_outcome"] = True
            return "degraded"
        return "block"
    accepted_choice = state.get("request", {}).get("accepted_degradation_choice") or {}
    current_choice = _accepted_choice_for_current_query_gaps(
        state,
        accepted_choice if isinstance(accepted_choice, Mapping) else {},
    )
    accepted_action = str(current_choice.get("action_kind") or "")
    if current_choice:
        state["current_query_gap_choice"] = current_choice
        state["accepted_choice_applicability"] = "applied_current_gap"
    elif result.status == "ready":
        state["accepted_choice_applicability"] = "recorded_preference"
    elif accepted_choice:
        state["accepted_choice_applicability"] = "requires_reconfirmation"
    if (
        accepted_action == "continue_with_boundary_only"
        and _has_authority_ready_independent_capability(
            result,
            tuple(dict(item) for item in getattr(result, "typed_gaps", ())),
        )
    ):
        current_choice = {
            **dict(current_choice),
            "action_kind": "omit_unavailable_context",
        }
        state["current_query_gap_choice"] = current_choice
        accepted_action = "omit_unavailable_context"
    if result.status == "clarify" and accepted_action in {
        "omit_unavailable_context",
        "continue_with_boundary_only",
    }:
        state["accepted_degraded_query_outcome"] = True
        return "degraded"
    if result.status == "clarify":
        return "clarify"
    if result.status == "recompile":
        return "recompile"
    if result.status == "ready":
        return "ready"
    if result.status == "degraded":
        return "degraded"
    return "block"


def _accepted_choice_for_current_query_gaps(
    state: Mapping[str, Any],
    accepted_choice: Mapping[str, Any],
) -> dict[str, Any]:
    if not accepted_choice:
        return {}
    result = state.get("analysis_runtime_result")
    typed_gaps = tuple(
        dict(item)
        for item in getattr(result, "typed_gaps", ())
        if isinstance(item, Mapping)
    )
    accepted_capabilities = tuple(
        state.get("analysis_route", {}).get("requested_nodes") or ()
    )
    registry = state.get("request", {}).get("runtime_registry")
    if not isinstance(registry, RuntimeContractRegistry):
        registry = RuntimeContractRegistry.from_path(
            CANONICAL_RUNTIME_BINDINGS_PATH
        )
    business_gaps = _business_query_gap_projection(
        typed_gaps,
        state.get("intent", {}),
        accepted_capabilities=accepted_capabilities,
        registry=registry,
    )
    repair_gap = _business_query_repair_gap(
        state.get("query_repair_decisions") or ()
    )
    if repair_gap:
        business_gaps.append(repair_gap)
    current, staged = _group_query_gap_actions(
        tuple(
            dict(action)
            for gap in business_gaps
            for action in gap.get("allowed_actions") or ()
            if isinstance(action, Mapping)
        )
    )
    accepted_id = str(accepted_choice.get("choice_id") or "")
    accepted_label = (
        accepted_choice.get("business_semantics")
        or accepted_choice.get("business_label")
    )
    accepted_kind = str(accepted_choice.get("action_kind") or "")
    accepted_affected = {
        str(item)
        for item in accepted_choice.get("affected_capabilities") or ()
        if str(item)
    }
    for action in (*current, *staged):
        exact_id = accepted_id and action.get("choice_id") == accepted_id
        same_business_choice = (
            str(action.get("action_kind") or "") == accepted_kind
            and clarification_labels_match(
                action.get("business_semantics"), accepted_label
            )
            and {
                str(item)
                for item in action.get("affected_capabilities") or ()
                if str(item)
            }
            == accepted_affected
        )
        if exact_id or same_business_choice:
            return {**dict(accepted_choice), **dict(action)}
    return {}


def _repair_analysis_contract(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "repair_analysis_contract")
    decisions = tuple(state.get("query_repair_decisions") or ())
    attempted = list(state.get("request", {}).get("attempted_query_signatures") or ())
    for decision in decisions:
        signature = str(decision.get("failed_signature") or "")
        if signature and signature not in attempted:
            attempted.append(signature)
    state["request"]["attempted_query_signatures"] = tuple(attempted)
    state["request"]["analysis_repair_reasons"] = tuple(
        str(item.get("reason") or "") for item in decisions if item.get("reason")
    )
    state["request"]["analysis_repair_feedback"] = decisions
    return _repair_analysis_route(state)


def _generate_query_gap_clarification(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "generate_query_gap_clarification")
    result = state.get("analysis_runtime_result")
    typed_gaps = [dict(item) for item in (result.typed_gaps if result is not None else ())]
    accepted_capabilities = tuple(
        state.get("compiled_graph").mutations.accepted_graph
        if state.get("compiled_graph") is not None
        else state.get("analysis_route", {}).get("requested_nodes") or ()
    )
    registry = state.get("request", {}).get("runtime_registry")
    if not isinstance(registry, RuntimeContractRegistry):
        registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    business_gaps = _business_query_gap_projection(
        typed_gaps,
        state.get("intent", {}),
        accepted_capabilities=accepted_capabilities,
        registry=registry,
    )
    if not any(gap.get("allowed_actions") for gap in business_gaps):
        repair_gap = _business_query_repair_gap(
            state.get("query_repair_decisions") or ()
        )
        if repair_gap:
            business_gaps.append(repair_gap)
    if not any(gap.get("allowed_actions") for gap in business_gaps):
        state["query_gap_no_feasible_action"] = True
        state["workflow_status"] = "blocked"
        return state
    selected_actions, staged_actions = _group_query_gap_actions(
        [
            dict(action)
            for gap in business_gaps
            for action in gap.get("allowed_actions") or ()
            if isinstance(action, Mapping)
        ]
    )
    if not 1 <= len(selected_actions) <= 2:
        raise WorkflowFailure(
            f"query_gap_action_contract_invalid:action_count:{len(selected_actions)}",
            failure_type="contract",
        )
    state["staged_query_gap_actions"] = staged_actions
    forced_ready_sibling_option = ""
    if _has_authority_ready_independent_capability(result, typed_gaps):
        forced_ready_sibling_option = next(
            (
                str(action.get("business_semantics") or "")
                for action in selected_actions
                if action.get("action_kind") == "omit_unavailable_context"
            ),
            "",
        )
        if not forced_ready_sibling_option:
            raise WorkflowFailure(
                "query_gap_ready_sibling_action_missing",
                failure_type="contract",
            )
    output = _invoke_llm(
        state,
        "query_gap_clarification",
        {
            "business_gaps": _query_gap_prompt_business_gaps(business_gaps),
            "repair_statuses": _business_query_repair_statuses(
                state.get("query_repair_decisions") or (),
            ),
            "business_labels": {
                "metric": state.get("intent", {}).get("target_metric"),
                "time_window": state.get("intent", {}).get("time_window"),
            },
            "allowed_business_options": [
                str(action["business_semantics"])
                for action in selected_actions
            ],
            "recommended_business_option": forced_ready_sibling_option,
        },
        output_validator=_validate_query_gap_clarification_provider_output,
    )
    questions = output.get("questions")
    if not isinstance(questions, Sequence) or isinstance(questions, (str, bytes)):
        raise WorkflowFailure(
            "query_gap_clarification_contract_invalid",
            failure_type="llm_contract",
        )
    first_question = next(
        (
            dict(question)
            for question in questions
            if isinstance(question, Mapping)
            and str(question.get("question") or "").strip()
        ),
        {},
    )
    if not first_question:
        raise WorkflowFailure(
            "query_gap_clarification_contract_invalid:question_missing",
            failure_type="llm_contract",
        )
    if _query_gap_clarification_internal_authority_leaks(output, typed_gaps):
        raise WorkflowFailure(
            "query_gap_clarification_internal_authority_leak",
            failure_type="llm_contract",
        )
    options, choice_actions = _render_query_gap_actions(
        state,
        business_gaps,
        output=output,
        forced_recommended_option=forced_ready_sibling_option,
    )
    first_question["options"] = options
    output["questions"] = [first_question]
    output["choice_actions"] = choice_actions
    state["query_gap_clarification"] = output
    state["workflow_status"] = "waiting_for_clarification"
    return state


def _has_authority_ready_independent_capability(
    result: Any,
    typed_gaps: Sequence[Mapping[str, Any]],
) -> bool:
    """Return true when a verified claim-ready binding is outside every material gap."""

    if result is None:
        return False
    material_gaps = tuple(
        gap for gap in typed_gaps if bool(gap.get("requires_clarification"))
    )
    if any(not tuple(gap.get("affected_capabilities") or ()) for gap in material_gaps):
        return False
    affected = {
        str(capability)
        for gap in material_gaps
        for capability in gap.get("affected_capabilities") or ()
        if str(capability)
    }
    bound_inputs = getattr(result, "bound_capability_inputs", {}) or {}
    return any(
        str(capability) not in affected
        and capability_binding_claim_ready(bound)
        and bool(getattr(bound, "binding_manifest_ref", ""))
        for capability, bound in bound_inputs.items()
    )


def _route_after_query_gap_clarification(state: WorkflowState) -> str:
    return "block" if state.get("query_gap_no_feasible_action") else "wait"


def _query_gap_prompt_business_gaps(
    business_gaps: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    projected = []
    for gap in business_gaps:
        projected.append(
            {
                "business_gap": gap.get("business_gap"),
                "business_impact": gap.get("business_impact"),
                "owner": gap.get("owner"),
                "allowed_actions": [
                    {
                        "choice_id": action.get("choice_id"),
                        "action_kind": action.get("action_kind"),
                        "business_semantics": action.get("business_semantics"),
                    }
                    for action in gap.get("allowed_actions") or ()
                    if isinstance(action, Mapping)
                ],
            }
        )
    return projected


def _render_query_gap_actions(
    state: WorkflowState,
    business_gaps: Sequence[Mapping[str, Any]],
    *,
    output: dict[str, Any],
    forced_recommended_option: str = "",
) -> tuple[list[str], list[dict[str, Any]]]:
    raw_actions: list[dict[str, Any]] = []
    for gap in business_gaps:
        for action in gap.get("allowed_actions") or ():
            if not isinstance(action, Mapping):
                continue
            raw_actions.append(dict(action))
    grouped_actions, staged_actions = _group_query_gap_actions(raw_actions)
    state["staged_query_gap_actions"] = staged_actions
    if not 1 <= len(grouped_actions) <= 2:
        raise WorkflowFailure(
            f"query_gap_action_contract_invalid:action_count:{len(grouped_actions)}",
            failure_type="contract",
        )
    questions = output.get("questions") or ()
    if (
        not isinstance(questions, Sequence)
        or isinstance(questions, (str, bytes))
        or len(questions) != 1
        or not isinstance(questions[0], Mapping)
    ):
        raise WorkflowFailure(
            "query_gap_action_binding_invalid:questions",
            failure_type="llm_contract",
        )
    expected_business_options = tuple(
        str(action["business_semantics"]) for action in grouped_actions
    )
    options = [*expected_business_options, CLARIFICATION_ESCAPE_OPTION]
    raw_options = questions[0].get("options")
    advisory_risks = list(output.get("advisory_risks") or ())
    if canonical_value(raw_options) != canonical_value(options):
        advisory_risks.append("provider_query_gap_options_ignored")
    recommendation_reason = str(output.get("recommendation_reason") or "").strip()
    if not recommendation_reason:
        raise WorkflowFailure(
            "query_gap_action_binding_invalid:recommendation_reason",
            failure_type="llm_contract",
        )
    recommended = output.get("recommended_assumption") or {}
    recommended_option = (
        recommended.get("option") if isinstance(recommended, Mapping) else None
    )
    if forced_recommended_option:
        if forced_recommended_option not in expected_business_options:
            raise WorkflowFailure(
                "query_gap_ready_sibling_action_missing",
                failure_type="contract",
            )
        selected_option = forced_recommended_option
    elif (
        isinstance(recommended_option, str)
        and recommended_option in expected_business_options
    ):
        selected_option = recommended_option
    else:
        selected_option = expected_business_options[0]
    if recommended_option != selected_option:
        advisory_risks.append(
            "provider_query_gap_recommendation_overridden"
        )
    output["recommended_assumption"] = {"option": selected_option}
    if advisory_risks:
        output["advisory_risks"] = list(dict.fromkeys(advisory_risks))
    elif "advisory_risks" in output:
        output["advisory_risks"] = []
    if not selected_option:
        raise WorkflowFailure(
            "query_gap_action_binding_invalid:recommended_option",
            failure_type="llm_contract",
        )
    actions_by_semantics = {
        str(action["business_semantics"]): action for action in grouped_actions
    }
    bound = [
        {
            **actions_by_semantics[option],
            "business_label": option,
            "business_reason": recommendation_reason
            if option == selected_option
            else "",
        }
        for option in expected_business_options
    ]
    bound.append(
        {
            "choice_id": "user_redirect",
            "action_kind": "user_redirect",
            "business_label": CLARIFICATION_ESCAPE_OPTION,
            "business_reason": "允许用户提供新的处理方式",
            "affected_capabilities": [],
        }
    )
    selected_choice_id = str(
        actions_by_semantics[selected_option].get("choice_id") or ""
    )
    projected = project_clarification_recommendation(
        {
            **output,
            "questions": [
                {
                    **dict(questions[0]),
                    "options": options,
                }
            ],
            "choice_actions": bound,
        },
        recommended_choice_id=selected_choice_id,
    )
    output.update(projected)
    return (
        list(projected["questions"][0]["options"]),
        list(projected["choice_actions"]),
    )


def _validate_query_gap_clarification_provider_output(
    output: Mapping[str, Any],
) -> None:
    questions = output.get("questions")
    if (
        not isinstance(questions, Sequence)
        or isinstance(questions, (str, bytes))
        or len(questions) != 1
        or not isinstance(questions[0], Mapping)
        or not isinstance(questions[0].get("question"), str)
        or not str(questions[0].get("question") or "").strip()
    ):
        raise LLMOutputError(
            "query_gap_clarification_contract_invalid:question_missing"
        )
    recommendation_reason = output.get("recommendation_reason")
    if (
        not isinstance(recommendation_reason, str)
        or not recommendation_reason.strip()
    ):
        raise LLMOutputError(
            "query_gap_action_binding_invalid:recommendation_reason"
        )


_REVIEWED_QUERY_GAP_ACTION_KINDS = frozenset(
    {
        "omit_unavailable_context",
        "continue_with_boundary_only",
        "wait_for_source",
        "wait_for_snapshot_availability",
        "register_dataset_snapshot",
        "bind_source",
        "choose_supported_window",
        "choose_supported_claim_intent",
        "clarify_window_contract",
        "use_supported_grain",
        "remove_dimension_path",
    }
)


def _group_query_gap_actions(
    actions: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in actions:
        action_kind = str(raw.get("action_kind") or "").strip()
        outcome = str(raw.get("business_semantics") or "").strip()
        if not action_kind or not outcome:
            continue
        if action_kind not in _REVIEWED_QUERY_GAP_ACTION_KINDS:
            raise WorkflowFailure(
                f"query_gap_action_contract_invalid:unknown_action:{action_kind}",
                failure_type="contract",
            )
        key = (action_kind, outcome)
        affected = {
            str(item)
            for item in raw.get("affected_capabilities") or ()
            if str(item)
        }
        if key not in grouped:
            grouped[key] = {
                **dict(raw),
                "action_kind": action_kind,
                "business_semantics": outcome,
                "affected_capabilities": sorted(affected),
            }
        else:
            grouped[key]["affected_capabilities"] = sorted(
                {
                    *grouped[key].get("affected_capabilities", ()),
                    *affected,
                }
            )
    priority = {
        "omit_unavailable_context": 0,
        "continue_with_boundary_only": 0,
        "wait_for_source": 1,
    }
    ordered = sorted(
        grouped.values(),
        key=lambda item: (
            priority.get(str(item.get("action_kind") or ""), 2),
            str(item.get("action_kind") or ""),
            str(item.get("business_semantics") or ""),
        ),
    )
    for item in ordered:
        identity = "|".join(
            (
                str(item["action_kind"]),
                ",".join(item.get("affected_capabilities") or ()),
                str(item["business_semantics"]),
            )
        )
        item["choice_id"] = f"query-gap-{sha256(identity.encode('utf-8')).hexdigest()[:16]}"
    return ordered[:2], ordered[2:]


def _business_query_gap_projection(
    typed_gaps: Sequence[Mapping[str, Any]],
    intent: Mapping[str, Any],
    *,
    accepted_capabilities: Sequence[str] = (),
    registry: RuntimeContractRegistry | None = None,
) -> list[dict[str, Any]]:
    gap_labels = {
        "dataset_snapshot_unavailable_as_of": "业务数据在分析时点尚不可用",
        "unsupported_grain": "请求的业务明细层级不受支持",
        "source_unbound": "业务数据来源尚未绑定",
        "contract_absent": "业务数据合同缺失",
        "contract_partial": "业务数据合同不完整",
        "query_completeness_failed": "查询结果完整性未通过",
    }
    owner_labels = {
        "data_owner": "数据负责人",
        "contract_owner": "数据合同负责人",
        "analysis_owner": "分析负责人",
    }
    repair_categories = {
        "wait_for_snapshot_availability": "等待业务数据可用后继续",
        "register_dataset_snapshot": "由数据负责人补齐业务数据",
        "bind_source": "由数据合同负责人绑定业务来源",
        "choose_supported_window": "改用受支持的业务时间窗口",
        "choose_supported_claim_intent": "改用受支持的结论口径",
        "clarify_window_contract": "确认业务时间口径",
        "use_supported_grain": "改用受支持的业务明细层级",
        "remove_dimension_path": "取消该明细拆分",
    }
    metric = str(intent.get("target_metric") or "目标指标").strip()
    time_window = str(intent.get("time_window") or "目标时间范围").strip()
    projected: list[dict[str, Any]] = []
    for gap in typed_gaps:
        if not bool(gap.get("requires_clarification")):
            continue
        gap_type = str(gap.get("gap_type") or "").strip()
        affected_capabilities = tuple(
            str(item) for item in gap.get("affected_capabilities") or () if str(item)
        )
        actions: list[dict[str, Any]] = []
        if gap_type in {
            "dataset_snapshot_unavailable_as_of",
            "source_unbound",
            "contract_partial",
        }:
            independent_capabilities = tuple(
                capability
                for capability in accepted_capabilities
                if capability not in affected_capabilities
            )
            if independent_capabilities:
                actions.append(
                    {
                        "choice_id": "continue_without_unavailable_context",
                        "action_kind": "omit_unavailable_context",
                        "business_semantics": "继续可验证的主指标分析，并明确缺少相关业务背景证据",
                        "affected_capabilities": list(affected_capabilities),
                    }
                )
            elif not independent_capabilities:
                actions.append(
                    {
                        "choice_id": "continue_with_boundary_only",
                        "action_kind": "continue_with_boundary_only",
                        "business_semantics": "基于当前证据边界完成限制说明，不发布业务结论",
                        "affected_capabilities": list(affected_capabilities),
                    }
                )
            actions.append(
                {
                    "choice_id": "wait_for_business_data",
                    "action_kind": "wait_for_source",
                    "business_semantics": "等待相关业务数据可用后再恢复本次分析",
                    "affected_capabilities": list(affected_capabilities),
                }
            )
        else:
            actions.extend(
                {
                    "choice_id": f"repair_{index}",
                    "action_kind": option,
                    "business_semantics": repair_categories[option],
                    "affected_capabilities": list(affected_capabilities),
                }
                for index, option in enumerate(gap.get("repair_options") or (), start=1)
                if option in repair_categories
            )
        projected.append(
            {
                "business_gap": gap_labels.get(gap_type, "业务分析前置条件未满足"),
                "business_impact": f"可能改变{metric}在{time_window}下的结论或证据强度",
                "owner": owner_labels.get(str(gap.get("owner") or ""), "分析负责人"),
                "allowed_actions": actions,
            }
        )
    return projected


def _business_query_repair_statuses(
    decisions: Sequence[Mapping[str, Any]],
) -> list[str]:
    allowed = {"retry", "recompile", "clarify", "degrade", "block"}
    return list(
        dict.fromkeys(
            status
            for decision in decisions
            for status in (str(decision.get("action") or "").strip(),)
            if status in allowed
        )
    )


def _business_query_repair_gap(
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    material = [
        decision
        for decision in decisions
        if bool(decision.get("requires_clarification"))
        and str(decision.get("action") or "") == "clarify"
    ]
    if not material:
        return {}
    reasons = {str(decision.get("reason") or "") for decision in material}
    if reasons == {"window_coverage_failure"}:
        return {
            "business_gap": "固定目标业务窗口的数据覆盖尚不完整",
            "business_impact": "当前不能改变目标日，也不能发布不完整的比较结论",
            "owner": "数据负责人",
            "allowed_actions": [
                {
                    "choice_id": "continue_with_available_fixed_window_evidence",
                    "action_kind": "omit_unavailable_context",
                    "business_semantics": "保留固定目标窗口，使用当前可验证证据完成受限结论",
                    "affected_capabilities": [],
                },
                {
                    "choice_id": "wait_for_fixed_window_data",
                    "action_kind": "wait_for_source",
                    "business_semantics": "等待固定目标业务窗口数据完整后继续，不调整目标日期",
                    "affected_capabilities": [],
                }
            ],
        }
    return {}


def _query_gap_clarification_internal_authority_leaks(
    output: Mapping[str, Any],
    typed_gaps: Sequence[Mapping[str, Any]],
) -> bool:
    rendered = json.dumps(to_jsonable(output), ensure_ascii=False).lower()
    forbidden = {"数据集", "快照", "snapshot", " utc", "utc)"}
    for gap in typed_gaps:
        for key in ("dataset_id", "gap_id"):
            value = str(gap.get(key) or "").strip().lower()
            if value:
                forbidden.add(value)
        diagnostics = gap.get("diagnostic_context") or {}
        if isinstance(diagnostics, Mapping):
            for key, raw_value in diagnostics.items():
                if not str(key).startswith("earliest_"):
                    continue
                value = str(raw_value or "").strip().lower()
                if value:
                    forbidden.add(value)
                    forbidden.add(value[:10])
    return any(value and value in rendered for value in forbidden)


def _persist_query_gap_clarification(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "persist_query_gap_clarification")
    result = state.get("analysis_runtime_result")
    package = {
        "run_id": state["run_id"],
        "status": "waiting_for_clarification",
        "clarification": to_jsonable(state.get("query_gap_clarification") or {}),
        "accepted_graph": list(state.get("compiled_graph").mutations.accepted_graph),
        "analysis_route": to_jsonable(state.get("analysis_route") or {}),
        "original_intent": to_jsonable(state.get("intent") or {}),
        "material_slots": to_jsonable(
            _clarification_material_slots(state)
        ),
        "analysis_contract": (
            result.analysis_contract.to_dict() if result is not None else {}
        ),
        "execution_material": to_jsonable(
            state.get("execution_material")
        ),
        "query_contracts": (
            [item.to_dict() for item in result.query_contracts] if result is not None else []
        ),
        "repair_decisions": list(state.get("query_repair_decisions") or ()),
        "staged_query_gap_actions": to_jsonable(
            state.get("staged_query_gap_actions") or ()
        ),
        "llm_calls": to_jsonable(state.get("llm_calls") or ()),
        "checkpoint_events": to_jsonable(
            state.get("checkpoint_events") or ()
        ),
    }
    state["workflow_status"] = "waiting_for_clarification"
    state["answer_package"] = package
    state["artifact_path"] = persist_artifact(
        package,
        artifact_root=state["request"].get("artifact_root", "artifacts/phase-4"),
    )
    return state


def _clarification_material_slots(state: Mapping[str, Any]) -> dict[str, Any]:
    slots = _intent_material_slots(state.get("intent") or {})
    requirements = (state.get("analysis_route") or {}).get(
        "analysis_requirements"
    ) or {}
    if not isinstance(requirements, Mapping):
        return slots
    for key in (*MATERIAL_AUTHORITY_LIST_AXES, "diagnostic_tags"):
        if key in requirements:
            slots[key] = to_jsonable(requirements[key])
    scope = requirements.get("scope")
    if scope not in (None, "", (), [], {}):
        slots["scope"] = to_jsonable(scope)
    return slots


def _interpret_data_coverage(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "interpret_data_coverage")
    coverage_rows = _coverage_rows_for_local_check(state)
    coverage_payload = {
        "intent": state["intent"],
        "schema_summary": state["schema"],
        "data_result_summary": _data_result_summary(coverage_rows),
        "validator_results": state["validator_results"],
        "sql_hash": state["sql_hash"],
    }
    block_reason = _local_coverage_block_reason(state)
    answerable_reason = _local_coverage_answerable_reason(state)
    try:
        coverage = _invoke_llm(
            state,
            "data_coverage_interpretation",
            coverage_payload,
        )
    except WorkflowFailure as exc:
        coverage = _deterministic_coverage_interpretation(
            block_reason=block_reason,
            answerable_reason=answerable_reason,
            fallback_reason=_exception_reason(exc),
        )
        state["llm_calls"].append(
            _local_llm_decision_audit(
                task="data_coverage_interpretation",
                payload=coverage_payload,
                output=coverage,
                reason="coverage_narrative_provider_fallback",
            )
        )
    if block_reason:
        coverage = {
            **coverage,
            "coverage_status": "blocked",
            "business_impact": _business_limitation_reasons((block_reason,))[0],
            "decision_summary": "本地覆盖检查发现硬边界，不能发布主业务结论。",
            "local_block_reason": block_reason,
        }
    elif coverage.get("coverage_status") == "blocked":
        if answerable_reason:
            coverage = {
                **coverage,
                "coverage_status": "coverage_gap_but_answerable",
                "business_impact": answerable_reason,
                "decision_summary": "本地聚合结果已经满足当前问题的执行口径，继续进入证据计算，并在答案里保留可见边界。",
                "local_override": "blocked_without_local_evidence",
            }
        else:
            coverage = {
                **coverage,
                "coverage_status": "sufficient",
                "local_override": "blocked_without_local_evidence",
            }
    elif coverage.get("coverage_status") in {"needs_question", "coverage_gap_but_answerable"}:
        if answerable_reason and (
            coverage.get("coverage_status") == "needs_question"
            or _coverage_text_requests_confirmation(coverage)
        ):
            coverage = {
                **coverage,
                "coverage_status": "coverage_gap_but_answerable",
                "business_impact": answerable_reason,
                "decision_summary": "本地聚合结果已经满足当前问题的执行口径，继续进入证据计算，并在答案里保留可见边界。",
                "local_override": "needs_question_without_local_gap",
            }
    state["coverage_interpretation"] = coverage
    return state


def _deterministic_coverage_interpretation(
    *,
    block_reason: str,
    answerable_reason: str,
    fallback_reason: str,
) -> dict[str, Any]:
    if block_reason:
        status = "blocked"
        business_impact = _business_limitation_reasons((block_reason,))[0]
        decision_summary = "本地完整性检查发现硬边界，当前不能发布主业务结论。"
    elif answerable_reason:
        status = "coverage_gap_but_answerable"
        business_impact = answerable_reason
        decision_summary = (
            "核心比较数据已通过本地完整性检查，可以继续计算；"
            "辅助数据缺口将在最终答案中单独说明。"
        )
    else:
        status = "sufficient"
        business_impact = (
            "目标窗口与已确认基线的聚合数据已通过本地完整性检查，"
            "可以继续验证变化方向并执行后续分析。"
        )
        decision_summary = "数据覆盖满足当前业务问题，继续进入证据计算。"
    return {
        "coverage_status": status,
        "business_impact": business_impact,
        "decision_summary": decision_summary,
        "display_summary": decision_summary,
        "local_narrative_fallback": True,
        "fallback_reason": fallback_reason,
    }


def _data_result_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fields = list(rows[0].keys()) if rows else []
    value_counts = {}
    for field in fields:
        values = {str(row.get(field)) for row in rows if field in row}
        if len(values) <= 30:
            value_counts[field] = sorted(values)
        else:
            value_counts[field] = {"distinct_count": len(values)}
    return {
        "row_count": len(rows),
        "fields": fields,
        "field_values": value_counts,
    }


def _formula_candidate_framework(
    state: WorkflowState,
    *,
    available_runtime_metrics: Iterable[str] = (),
    available_dimensions: Iterable[str] = (),
) -> dict[str, Any]:
    request = state.get("request") or {}
    registry = request.get("runtime_registry")
    if not isinstance(registry, RuntimeContractRegistry):
        registry = RuntimeContractRegistry.from_path(
            CANONICAL_RUNTIME_BINDINGS_PATH
        )
    target_metric = str(state.get("intent", {}).get("target_metric") or "")
    metric_binding = registry.metric(target_metric)
    contract_ref = str(metric_binding.get("contract_ref") or "")
    if not contract_ref:
        raise WorkflowFailure(
            f"formula_metric_contract_ref_missing:{target_metric}",
            failure_type="contract",
        )
    contract_path_text = contract_ref.split("|", 1)[0].split("#", 1)[0]
    if "@" in contract_path_text:
        contract_path_text = contract_path_text.rsplit("@", 1)[0]
    contract_path = Path(contract_path_text)
    if not contract_path.is_absolute():
        contract_path = Path(__file__).resolve().parents[2] / contract_path
    framework = build_formula_candidate_framework(
        metric_contract_path=contract_path,
        available_runtime_metrics=available_runtime_metrics,
        available_dimensions=available_dimensions,
        requested_components=tuple(
            str(item)
            for item in state.get("intent", {}).get("component_ids", ())
            if str(item)
        ),
    )
    framework["metric_contract_ref"] = contract_ref
    return framework


def _attach_formula_candidate_framework(
    state: WorkflowState,
    *,
    available_runtime_metrics: Iterable[str] = (),
    available_dimensions: Iterable[str] = (),
) -> dict[str, Any]:
    framework = _formula_candidate_framework(
        state,
        available_runtime_metrics=available_runtime_metrics,
        available_dimensions=available_dimensions,
    )
    runtime_plan = {
        **dict(state.get("request", {}).get("compiler_runtime_plan") or {}),
        "formula_candidate_framework": framework,
    }
    state.setdefault("request", {})["compiler_runtime_plan"] = runtime_plan
    compiled = state.get("compiled_graph")
    if compiled is not None and is_dataclass(compiled) and hasattr(
        compiled, "runtime_plan"
    ):
        state["compiled_graph"] = replace(
            compiled,
            runtime_plan={
                **dict(compiled.runtime_plan),
                "formula_candidate_framework": framework,
            },
        )
    return framework


def _execute_capabilities(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "execute_capabilities")
    evidence = []
    compiled = state["compiled_graph"]
    capabilities = tuple(compiled.mutations.accepted_graph)
    bound_inputs = state.get("request", {}).get("bound_capability_inputs")
    if (
        state.get("request", {}).get("runtime_rows_source") == "analysis_runtime"
        and isinstance(bound_inputs, Mapping)
    ):
        capabilities = tuple(
            capability for capability in capabilities if capability in bound_inputs
        )
    budget = state.get("budget_state") or default_budget("ordinary")

    for capability_id in (
        capability
        for capability in capabilities
        if capability in WINDOW_METRIC_COMPARE_CAPABILITIES
    ):
        bound = (state.get("request", {}).get("bound_capability_inputs") or {}).get(
            capability_id
        )
        supported_claim_types = tuple(
            getattr(bound, "supported_claim_types", ()) or ()
        )
        selected_baselines = _claim_scoped_baseline_ids(state, capability_id)
        window_params = (
            {"primary_baseline_window_id": selected_baselines[0]}
            if selected_baselines is not None and len(selected_baselines) == 1
            else {}
        )
        evidence.append(
            execute_capability(
                CapabilityRequest(
                    run_id=state["run_id"],
                    accepted_graph_id=f"{state['run_id']}:accepted_graph",
                    graph_version=1,
                    capability_id=capability_id,
                    question_family=state["intent"]["question_family"],
                    target_claim=state["intent"].get("target_claim", ""),
                    claim_type=(
                        supported_claim_types[0]
                        if supported_claim_types
                        else "comparative_change"
                    ),
                    metric=state["intent"]["target_metric"],
                    scope=state["intent"]["scope"],
                    time_window=state["intent"]["time_window"],
                    baseline=state["intent"].get("baseline") or {"label": "baseline"},
                    target=state["intent"].get("target") or {"label": "target"},
                    grain="window",
                    filters={},
                    dimensions=(),
                    contract_versions={},
                    budget_state=budget,
                    llm_business_reason="执行已接受的窗口指标对比能力。",
                    params=window_params,
                    **_capability_authority_inputs(state, capability_id),
                )
            )
        )
        budget = record_capability_call(budget)

    if "data_quality_profile" in capabilities:
        capability_rows = _capability_rows_for(state, "data_quality_profile")
        capability_refs = _capability_result_refs_for(state, "data_quality_profile")
        evidence.append(
            execute_capability(
                CapabilityRequest(
                    run_id=state["run_id"],
                    accepted_graph_id=f"{state['run_id']}:accepted_graph",
                    graph_version=1,
                    capability_id="data_quality_profile",
                    question_family=state["intent"]["question_family"],
                    target_claim=state["intent"].get("target_claim", ""),
                    claim_type="contract_coverage_and_trust_boundary",
                    metric=state["intent"]["target_metric"],
                    scope=state["intent"]["scope"],
                    time_window=state["intent"]["time_window"],
                    baseline=state["intent"].get("baseline", {}),
                    target=state["intent"].get("target", {}),
                    grain=state["intent"]["pattern_family"],
                    filters={},
                    dimensions=(),
                    contract_versions={},
                    budget_state=budget,
                    llm_business_reason="检查本次聚合结果是否足以支撑业务判断。",
                    params={
                        "rows": capability_rows,
                        "result_refs": capability_refs,
                        "required_fields": _capability_required_fields(
                            state,
                            "data_quality_profile",
                        ),
                    },
                    **_capability_authority_inputs(state, "data_quality_profile"),
                )
            )
        )
        budget = record_capability_call(budget)

    for capability_id in (
        capability
        for capability in capabilities
        if capability in PATTERN_COMPARE_CAPABILITIES
    ):
        pattern_family = state["intent"]["pattern_family"]
        pattern_params = dict(state["intent"].get("pattern_params", {}))
        capability_rows, pattern_params = _comparison_rows_and_params(
            state,
            capability_id,
            params=pattern_params,
            dimension_keys=(),
            period_key=pattern_params.get("period_key", "period"),
        )
        capability_refs = _capability_result_refs_for(state, capability_id)
        target = state["intent"].get("target") or {
            "label": _target_label_from_pattern_params(pattern_params)
        }
        baseline = state["intent"].get("baseline") or {
            "label": _baseline_label_from_pattern_params(pattern_params)
        }
        evidence.append(
            execute_capability(
                CapabilityRequest(
                    run_id=state["run_id"],
                    accepted_graph_id=f"{state['run_id']}:accepted_graph",
                    graph_version=1,
                    capability_id=capability_id,
                    question_family=state["intent"]["question_family"],
                    target_claim=state["intent"].get("target_claim", ""),
                    claim_type=_capability_claim_type(state, capability_id),
                    metric=state["intent"]["target_metric"],
                    scope=state["intent"]["scope"],
                    time_window=state["intent"]["time_window"],
                    baseline=baseline,
                    target=target,
                    grain=pattern_family,
                    filters={},
                    dimensions=(),
                    contract_versions={},
                    budget_state=budget,
                    llm_business_reason="执行已接受的业务对比能力。",
                    params={
                        "rows": capability_rows,
                        "result_refs": capability_refs,
                        "pattern_family": pattern_family,
                        **pattern_params,
                    },
                    **_capability_authority_inputs(state, capability_id),
                )
            )
        )
        budget = record_capability_call(budget)
    state["budget_state"] = budget

    if "data_quality_check" in capabilities:
        capability_rows = _capability_rows_for(state, "data_quality_check")
        capability_refs = _capability_result_refs_for(state, "data_quality_check")
        evidence.append(
            data_quality_check(
                capability_rows,
                required_fields=tuple(
                    state["request"].get("required_fields", ("month", "phase", "amount"))
                ),
                result_refs=capability_refs,
            )
        )
    if "pattern_scan" in capabilities:
        pattern_family = state["intent"]["pattern_family"]
        pattern_params = dict(state["intent"].get("pattern_params", {}))
        capability_rows, pattern_params = _comparison_rows_and_params(
            state,
            "pattern_scan",
            params=pattern_params,
            dimension_keys=(),
            period_key=pattern_params.get("period_key", "period"),
        )
        capability_refs = _capability_result_refs_for(state, "pattern_scan")
        evidence.append(
            scan_pattern(
                capability_rows,
                pattern_family=pattern_family,
                materiality_floor=0.03,
                result_refs=capability_refs,
                evidence_ref=f"pattern_scan:{pattern_family}",
                **pattern_params,
            )
        )
    if "formula_decompose" in capabilities:
        formula_rows = _capability_rows_for(state, "formula_decompose")
        registry = state.get("request", {}).get("runtime_registry")
        if not isinstance(registry, RuntimeContractRegistry):
            registry = RuntimeContractRegistry.from_path(
                CANONICAL_RUNTIME_BINDINGS_PATH
            )
        row_fields = {
            str(field)
            for row in formula_rows
            for field in row
            if str(field)
        }
        available_components = tuple(
            metric for metric in registry.metric_ids if metric in row_fields
        )
        available_dimensions = tuple(
            dimension
            for dimension in registry.dimension_ids
            if dimension in row_fields
        )
        formula_framework = _attach_formula_candidate_framework(
            state,
            available_runtime_metrics=available_components,
            available_dimensions=available_dimensions,
        )
        formula_paths = [
            {
                "formula_id": candidate["path_id"],
                "components": tuple(candidate.get("runtime_components") or ()),
                **{
                    key: candidate[key]
                    for key in (
                        "candidate_role",
                        "candidate_status",
                        "candidate_rank",
                        "expression",
                        "ssot_node_id",
                        "path_role",
                        "target_component",
                        "contract_ref",
                        "matched_requested_components",
                        "missing_runtime_components",
                        "missing_dimensions",
                        "launch_status",
                    )
                    if key in candidate
                },
            }
            for candidate in formula_framework.get("candidates") or ()
        ]
        evidence.append(
            formula_decompose(
                formula_paths,
                available_components=available_components,
                result_refs=_capability_result_refs_for(state, "formula_decompose"),
            )
        )
    if "driver_decomposition" in capabilities:
        driver_params = _driver_params(state)
        capability_rows, driver_params = _comparison_rows_and_params(
            state,
            "driver_decomposition",
            params=driver_params,
            dimension_keys=(),
            period_key=driver_params.get("period_key", "period"),
        )
        capability_refs = _capability_result_refs_for(state, "driver_decomposition")
        target_window_id = _comparison_group_window_id(
            capability_rows,
            group_key=str(driver_params.get("group_key") or "group"),
            group_value=str(driver_params.get("target_group") or "target"),
        )
        baseline_window_id = _comparison_group_window_id(
            capability_rows,
            group_key=str(driver_params.get("group_key") or "group"),
            group_value=str(driver_params.get("baseline_group") or "baseline"),
        )
        evidence.append(
            driver_decomposition(
                capability_rows,
                result_refs=capability_refs,
                target_window_id=target_window_id,
                baseline_window_id=baseline_window_id,
                **driver_params,
            )
        )
    if "candidate_dimension_screen" in capabilities:
        evidence.append(
            candidate_dimension_screen(
                **_candidate_dimension_screen_params(
                    state,
                    prior_evidence=evidence,
                )
            )
        )
    if "segment_contribution" in capabilities:
        segment_params = _segment_contribution_params(state)
        capability_rows, segment_params = _comparison_rows_and_params(
            state,
            "segment_contribution",
            params=segment_params,
            dimension_keys=(segment_params.get("segment_key", "period"),),
        )
        capability_refs = _capability_result_refs_for(state, "segment_contribution")
        evidence.append(
            segment_contribution(
                capability_rows,
                result_refs=capability_refs,
                **segment_params,
            )
        )
    if "outlier_contribution" in capabilities:
        outlier_params = _outlier_contribution_params(state)
        capability_rows, outlier_params = _comparison_rows_and_params(
            state,
            "outlier_contribution",
            params=outlier_params,
            dimension_keys=(),
            period_key=outlier_params.get("period_key", "period"),
        )
        capability_refs = _capability_result_refs_for(state, "outlier_contribution")
        evidence.append(
            outlier_contribution(
                capability_rows,
                result_refs=capability_refs,
                **outlier_params,
            )
        )
    if "user_mix_contribution" in capabilities:
        capability_rows = _capability_rows_for(state, "user_mix_contribution")
        capability_refs = _capability_result_refs_for(state, "user_mix_contribution")
        evidence.append(
            user_mix_contribution(
                capability_rows,
                result_refs=capability_refs,
                **_user_mix_contribution_params(state),
            )
        )
    if "high_value_user_contribution" in capabilities:
        capability_rows = _capability_rows_for(state, "high_value_user_contribution")
        capability_refs = _capability_result_refs_for(state, "high_value_user_contribution")
        evidence.append(
            high_value_user_contribution(
                capability_rows,
                result_refs=capability_refs,
                **_high_value_user_contribution_params(state),
            )
        )
    if "event_evidence" in capabilities:
        evidence.append(
            execute_capability(
                CapabilityRequest(
                    run_id=state["run_id"],
                    accepted_graph_id=f"{state['run_id']}:accepted_graph",
                    graph_version=1,
                    capability_id="event_evidence",
                    question_family=state["intent"]["question_family"],
                    target_claim="candidate_mechanism",
                    claim_type="candidate_mechanism",
                    metric="",
                    scope=state["intent"]["scope"],
                    time_window=state["intent"]["time_window"],
                    baseline=state["intent"].get("baseline", {}),
                    target=state["intent"].get("target", {}),
                    grain="event_interval",
                    filters={},
                    dimensions=(),
                    contract_versions={},
                    budget_state=budget,
                    llm_business_reason="检查分析窗口内经过合同绑定的事件上下文。",
                    params={
                        "rows": _capability_rows_for(state, "event_evidence"),
                        "result_refs": _capability_result_refs_for(state, "event_evidence"),
                    },
                    **_capability_authority_inputs(state, "event_evidence"),
                )
            )
        )
        budget = record_capability_call(budget)
    temporal_association_evidence = None
    if "cross_source_association" in capabilities:
        temporal_association_evidence = execute_capability(
                CapabilityRequest(
                    run_id=state["run_id"],
                    accepted_graph_id=f"{state['run_id']}:accepted_graph",
                    graph_version=1,
                    capability_id="cross_source_association",
                    question_family=state["intent"]["question_family"],
                    target_claim="cross_source_statistical_association",
                    claim_type="cross_source_statistical_association",
                    metric="paid_amount",
                    scope=state["intent"]["scope"],
                    time_window=state["intent"]["time_window"],
                    baseline=state["intent"].get("baseline", {}),
                    target=state["intent"].get("target", {}),
                    grain="day",
                    filters={},
                    dimensions=(),
                    contract_versions={},
                    budget_state=budget,
                    llm_business_reason=(
                        "检验玩法经营指标与付费结果的同步、变化量及滞后关联，"
                        "并检查时间稳定性。"
                    ),
                    params={
                        "methods": ("pearson", "spearman"),
                        "transforms": (
                            "level",
                            "difference",
                            "signed_log_difference",
                        ),
                        "lags": tuple(range(-7, 8)),
                        "min_samples": 30,
                        "rolling_window": 90,
                        "min_rolling_windows": 3,
                        "fdr_method": "by",
                        "row_budget": 10000,
                    },
                    **_capability_authority_inputs(
                        state, "cross_source_association"
                    ),
                )
        )
        evidence.append(temporal_association_evidence)
        budget = record_capability_call(budget)
    if "cross_source_panel_association" in capabilities:
        evidence.append(
            execute_capability(
                CapabilityRequest(
                    run_id=state["run_id"],
                    accepted_graph_id=f"{state['run_id']}:accepted_graph",
                    graph_version=1,
                    capability_id="cross_source_panel_association",
                    question_family=state["intent"]["question_family"],
                    target_claim="cross_source_statistical_association",
                    claim_type="cross_source_statistical_association",
                    metric="paid_amount",
                    scope=state["intent"]["scope"],
                    time_window=state["intent"]["time_window"],
                    baseline=state["intent"].get("baseline", {}),
                    target=state["intent"].get("target", {}),
                    grain="channel_day",
                    filters={},
                    dimensions=("channel",),
                    contract_versions={},
                    budget_state=budget,
                    llm_business_reason=(
                        "扣除日期共同冲击和渠道长期体量差异后，检验玩法与付费"
                        "的关系能否在渠道内部重复出现。"
                    ),
                    params={
                        "hypotheses": _panel_hypotheses_from_temporal_association(
                            temporal_association_evidence
                        ),
                        "mapping_authority_status": (
                            "candidate_mechanical_crosswalk"
                        ),
                        "row_budget": 150000,
                    },
                    **_capability_authority_inputs(
                        state, "cross_source_panel_association"
                    ),
                )
            )
        )
        budget = record_capability_call(budget)
    state["budget_state"] = budget
    if "segment_bridge" in capabilities:
        segment = segment_bridge(
            _capability_rows_for(state, "segment_bridge"),
            result_refs=_capability_result_refs_for(state, "segment_bridge"),
        )
        evidence.append(segment)
    else:
        segment = None
    if "outlier_scan" in capabilities:
        evidence.append(
            outlier_scan(
                _capability_rows_for(state, "outlier_scan"),
                result_refs=_capability_result_refs_for(state, "outlier_scan"),
            )
        )
    if "joint_attribution" in capabilities:
        joint_params = _joint_attribution_params(state)
        capability_rows, joint_params = _comparison_rows_and_params(
            state,
            "joint_attribution",
            params=joint_params,
            dimension_keys=tuple(joint_params.get("dimension_keys") or ()),
        )
        capability_refs = _capability_result_refs_for(state, "joint_attribution")
        evidence.append(
            joint_attribution(
                capability_rows,
                segment_evidence=segment,
                result_refs=capability_refs,
                **joint_params,
            )
        )

    state["evidence"] = [_evidence_dict(item, state) for item in evidence]
    return state


def _panel_hypotheses_from_temporal_association(
    temporal_evidence: Any,
) -> tuple[dict[str, Any], ...]:
    """Carry robust temporal hypotheses into the channel-panel sensitivity check."""

    payload = getattr(temporal_evidence, "typed_payload", None)
    if not isinstance(payload, Mapping) and isinstance(temporal_evidence, Mapping):
        payload = temporal_evidence.get("typed_payload")
    if not isinstance(payload, Mapping):
        return ()
    associations_by_outcome = payload.get("associations_by_outcome")
    if not isinstance(associations_by_outcome, Mapping):
        return ()

    hypotheses: list[dict[str, Any]] = []
    for outcome_metric, outcome_bundle in associations_by_outcome.items():
        if not isinstance(outcome_bundle, Mapping):
            continue
        association = outcome_bundle.get("association")
        if not isinstance(association, Mapping):
            continue
        selected_candidates: set[str] = set()
        for estimate in association.get("supported_associations") or ():
            if not isinstance(estimate, Mapping):
                continue
            candidate_metric = str(estimate.get("candidate_key") or "")
            transform = str(estimate.get("transform") or "")
            lag = estimate.get("lag")
            rolling = estimate.get("rolling")
            if (
                not candidate_metric
                or candidate_metric in selected_candidates
                or transform == "level"
                or not isinstance(lag, int)
                or isinstance(lag, bool)
                or not isinstance(rolling, Mapping)
                or rolling.get("stable") is not True
                or estimate.get("supported") is False
            ):
                continue
            outcome = str(outcome_metric)
            hypotheses.append(
                {
                    "hypothesis_id": (
                        f"{outcome}:{candidate_metric}:{transform}:lag{lag}"
                    ),
                    "outcome_metric": outcome,
                    "candidate_metric": candidate_metric,
                    "transform": transform,
                    "lag": lag,
                }
            )
            selected_candidates.add(candidate_metric)
    return tuple(hypotheses)


def _capability_authority_inputs(
    state: WorkflowState,
    capability_id: str,
) -> dict[str, Any]:
    request = state.get("request") or {}
    bound_inputs = request.get("bound_capability_inputs")
    bound_input = (
        bound_inputs.get(capability_id)
        if isinstance(bound_inputs, Mapping)
        else None
    )
    return {
        "bound_input": bound_input,
        "evidence_resolver": request.get("evidence_resolver"),
        "rows_loader": request.get("rows_loader"),
        "runtime_registry": request.get("runtime_registry"),
        "release_resolver": request.get("release_resolver"),
    }


_EVIDENCE_STRENGTH_PRIORITY = {
    "high": 5,
    "medium": 4,
    "directional": 3,
    "low": 1,
    "insufficient": 0,
}

_EVIDENCE_WORDING_PRIORITY = {
    "supported": 7,
    "quantified": 6,
    "stable_association": 6,
    "contextual": 5,
    "candidate": 4,
    "candidate_association": 4,
    "candidate_mechanism_only": 3,
    "sensitivity_only": 1,
    "degraded": 2,
    "blocked": 0,
}


def _claim_evidence_selection_key(
    indexed_evidence: tuple[int, Mapping[str, Any]],
) -> tuple[int, int, int, int, int, int, int]:
    index, evidence = indexed_evidence
    limitations = tuple(evidence.get("limitations") or ())
    return (
        int(_evidence_established(dict(evidence))),
        int(_evidence_claim_input_ready(evidence)),
        _as_int(evidence.get("maximum_claim_strength_rank")),
        _EVIDENCE_STRENGTH_PRIORITY.get(str(evidence.get("strength") or ""), 0),
        _EVIDENCE_WORDING_PRIORITY.get(
            str(evidence.get("wording_limit") or ""),
            0,
        ),
        -len(limitations),
        -index,
    )


def _required_claim_evidence_resolution(state: WorkflowState) -> dict[str, Any]:
    """Resolve evidence per required claim without promoting auxiliary gaps."""

    intent = state.get("intent") or {}
    required_claims = tuple(
        dict.fromkeys(
            str(claim)
            for claim in (intent.get("required_claim_types") or ())
            if str(claim)
        )
    )
    candidate_claims = tuple(
        dict.fromkeys(
            str(claim)
            for claim in (intent.get("auxiliary_claim_types") or ())
            if str(claim)
        )
    )
    evidence_items = tuple(
        (index, item)
        for index, item in enumerate(state.get("evidence") or ())
        if isinstance(item, Mapping)
    )
    required: dict[str, dict[str, Any]] = {}
    selected_indexes: set[int] = set()
    for claim_type in required_claims:
        matching = tuple(
            indexed
            for indexed in evidence_items
            if str(indexed[1].get("claim_type") or "") == claim_type
        )
        selected = max(matching, key=_claim_evidence_selection_key) if matching else None
        if selected is not None:
            selected_indexes.add(selected[0])
        selected_evidence = dict(selected[1]) if selected is not None else None
        publishable = bool(
            selected_evidence
            and _evidence_claim_input_ready(selected_evidence)
            and _evidence_established(selected_evidence)
        )
        required[claim_type] = {
            "status": "publishable" if publishable else "unavailable",
            "evidence": selected_evidence,
        }

    publishable_required = tuple(
        entry["evidence"]
        for entry in required.values()
        if entry["status"] == "publishable" and entry.get("evidence")
    )
    selected_required = tuple(
        entry["evidence"]
        for entry in required.values()
        if entry.get("evidence")
    )
    primary_pool = publishable_required or selected_required
    primary = (
        max(
            enumerate(primary_pool),
            key=lambda item: _claim_evidence_selection_key(item),
        )[1]
        if primary_pool
        else {}
    )

    material_limitations: set[str] = set()
    for claim_type, entry in required.items():
        evidence = entry.get("evidence") or {}
        material_limitations.update(str(value) for value in evidence.get("limitations") or ())
        if not evidence:
            material_limitations.add(f"missing_required_claim_evidence:{claim_type}")

    auxiliary_limitations = {
        str(limitation)
        for index, evidence in evidence_items
        if index not in selected_indexes
        or str(evidence.get("claim_type") or "") in candidate_claims
        for limitation in evidence.get("limitations") or ()
    }
    auxiliary_limitation_scopes = []
    for index, evidence in evidence_items:
        claim_type = str(evidence.get("claim_type") or "")
        limitations = [
            str(limitation)
            for limitation in evidence.get("limitations") or ()
        ]
        if not limitations or (
            index in selected_indexes and claim_type not in candidate_claims
        ):
            continue
        if claim_type in candidate_claims:
            claim_role = "candidate_claim"
        elif claim_type in required_claims:
            claim_role = "superseded_required_evidence"
        else:
            claim_role = "auxiliary_evidence"
        auxiliary_limitation_scopes.append(
            {
                "evidence_ref": str(evidence.get("evidence_ref") or ""),
                "capability_id": str(
                    evidence.get("capability_id")
                    or evidence.get("capability")
                    or ""
                ),
                "claim_type": claim_type,
                "claim_role": claim_role,
                "limitations": limitations,
            }
        )
    return {
        "has_required_claims": bool(required_claims),
        "required": required,
        "candidate_claim_types": candidate_claims,
        "primary": dict(primary),
        "material_limitations": tuple(sorted(material_limitations)),
        "auxiliary_limitations": tuple(sorted(auxiliary_limitations)),
        "auxiliary_limitation_scopes": tuple(auxiliary_limitation_scopes),
    }


def _primary_answer_evidence(state: WorkflowState) -> dict[str, Any]:
    resolution = _required_claim_evidence_resolution(state)
    if resolution["has_required_claims"]:
        return dict(resolution.get("primary") or {})
    pattern = _pattern_evidence(state)
    return pattern or _primary_business_evidence(state)


def _reduce_evidence(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "reduce_evidence")
    resolution = _required_claim_evidence_resolution(state)
    primary = _primary_answer_evidence(state)
    pattern_ref = primary.get(
        "evidence_ref",
        f"pattern_scan:{state['intent']['pattern_family']}",
    )
    required_claim_evidence_refs = {
        claim_type: str(entry["evidence"].get("evidence_ref") or "")
        for claim_type, entry in resolution["required"].items()
        if entry["status"] == "publishable" and entry.get("evidence")
    }
    unready_required_claim_types = [
        claim_type
        for claim_type, entry in resolution["required"].items()
        if entry["status"] != "publishable"
    ]
    claim_evidence = {
        claim_type: {
            "role": "required",
            "status": entry["status"],
            "evidence_ref": (entry.get("evidence") or {}).get("evidence_ref"),
            "capability_id": (entry.get("evidence") or {}).get("capability_id")
            or (entry.get("evidence") or {}).get("capability"),
            "strength": (entry.get("evidence") or {}).get("strength"),
            "wording_limit": (entry.get("evidence") or {}).get("wording_limit"),
            "limitations": list((entry.get("evidence") or {}).get("limitations") or ()),
        }
        for claim_type, entry in resolution["required"].items()
    }
    state["evidence_brief"] = {
        "pattern_ref": pattern_ref,
        "pattern_status": primary.get("strength", "insufficient"),
        "pattern_established": _evidence_established(primary),
        "wording_limit": primary.get("wording_limit", "unknown"),
        "primary_capability": primary.get("capability_id") or primary.get("capability"),
        "claim_evidence": claim_evidence,
        "required_claim_evidence_refs": required_claim_evidence_refs,
        "unready_required_claim_types": unready_required_claim_types,
        "limitations": list(resolution["material_limitations"])
        if resolution["has_required_claims"]
        else sorted(
            {
                limitation
                for item in state.get("evidence", [])
                for limitation in item.get("limitations", ())
            }
        ),
        "auxiliary_limitations": list(resolution["auxiliary_limitations"]),
        "auxiliary_limitation_scopes": list(
            resolution["auxiliary_limitation_scopes"]
        ),
        "evidence_refs": [item.get("evidence_ref") for item in state.get("evidence", [])],
    }
    return state


def _build_diagnostic_insights(state: WorkflowState) -> WorkflowState:
    """Assemble decision-oriented insights from authority-accepted evidence."""

    _maybe_force_node_failure(state, "build_diagnostic_insights")
    intent = state.get("intent") or {}
    target = intent.get("target") or {}
    baseline = intent.get("baseline") or {}
    state["diagnostic_insights"] = build_diagnostic_insight_portfolio(
        question={
            "target_metric": intent.get("target_metric"),
            "target_window_id": (
                target.get("window_id")
                if isinstance(target, Mapping)
                else ""
            )
            or (target.get("id") if isinstance(target, Mapping) else ""),
            "baseline_window_id": (
                baseline.get("window_id")
                if isinstance(baseline, Mapping)
                else ""
            )
            or (baseline.get("id") if isinstance(baseline, Mapping) else ""),
        },
        evidence=tuple(
            dict(item)
            for item in state.get("evidence") or ()
            if isinstance(item, Mapping)
        ),
        available_routes=tuple(_diagnostic_available_routes(state)),
    )
    return state


def _diagnostic_available_routes(state: WorkflowState) -> list[dict[str, Any]]:
    """Collect only contract-projected routes that have not already produced evidence."""

    executed_capabilities = {
        str(item.get("capability_id") or item.get("capability") or "")
        for item in state.get("evidence") or ()
        if isinstance(item, Mapping)
    }
    sources = (
        state.get("request", {}).get("available_diagnostic_routes"),
        state.get("analysis_route", {}).get("available_diagnostic_routes"),
        state.get("analysis_route", {}).get("diagnostic_routes"),
    )
    routes: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, Sequence) or isinstance(source, (str, bytes)):
            continue
        for raw in source:
            if not isinstance(raw, Mapping):
                continue
            capability_id = str(raw.get("capability_id") or "")
            if capability_id and capability_id in executed_capabilities:
                continue
            route = dict(raw)
            route.setdefault(
                "route_id",
                capability_id or str(raw.get("candidate_id") or ""),
            )
            routes.append(route)
    return routes


def _capability_rows_for(
    state: WorkflowState,
    capability_id: str,
) -> Sequence[Mapping[str, Any]]:
    bound, limitation = _production_bound_input(state, capability_id)
    if limitation or bound is None:
        return ()
    return tuple(
        row
        for rows in bound.rows_by_slot.values()
        for row in rows
    )


def _capability_result_refs_for(state: WorkflowState, capability_id: str) -> tuple[str, ...]:
    bound, limitation = _production_bound_input(state, capability_id)
    if limitation or bound is None:
        return ()
    return tuple(
        dict.fromkeys((*bound.result_refs, *bound.validation_result_refs))
    )


def _production_bound_input(
    state: WorkflowState,
    capability_id: str,
) -> tuple[BoundCapabilityInput | None, str]:
    request = state.get("request", {})
    values = request.get("bound_capability_inputs") or {}
    bound = values.get(capability_id) if isinstance(values, Mapping) else None
    if not isinstance(bound, BoundCapabilityInput):
        return None, "missing_bound_capability_input"
    try:
        limitation = validate_bound_capability_input(
            bound,
            request.get("evidence_resolver"),
        )
    except Exception:
        limitation = "bound_capability_input_invalid"
    if limitation:
        return bound, str(limitation)
    if bound.status not in {"ready", "degraded"}:
        return bound, "bound_capability_input_blocked"
    return bound, ""


def _compiler_capability_query_intents(
    state: WorkflowState,
    capability_id: str,
) -> tuple[str, ...]:
    plan = state.get("request", {}).get("compiler_runtime_plan") or {}
    if isinstance(plan, Mapping):
        capability_inputs = plan.get("capability_inputs") or {}
        if isinstance(capability_inputs, Mapping):
            contract = capability_inputs.get(capability_id)
            if isinstance(contract, Mapping):
                intents = contract.get("preferred_query_intents") or ()
                if isinstance(intents, Sequence) and not isinstance(intents, (str, bytes)):
                    normalized = tuple(str(intent) for intent in intents if intent)
                    if normalized:
                        return normalized
    return _capability_query_intents(capability_id)


def _capability_required_fields(
    state: WorkflowState,
    capability_id: str,
) -> tuple[str, ...]:
    plans = state.get("request", {}).get("capability_execution_plans") or ()
    required_fields: list[str] = []
    for plan in plans:
        current_id = (
            plan.get("capability_id")
            if isinstance(plan, Mapping)
            else getattr(plan, "capability_id", "")
        )
        if str(current_id or "") != capability_id:
            continue
        slots = (
            plan.get("required_input_slots") or ()
            if isinstance(plan, Mapping)
            else getattr(plan, "required_input_slots", ())
        )
        for slot in slots:
            fields = (
                slot.get("required_fields") or ()
                if isinstance(slot, Mapping)
                else getattr(slot, "required_fields", ())
            )
            for field in fields:
                normalized = str(field or "")
                if normalized and normalized not in required_fields:
                    required_fields.append(normalized)
        break
    if required_fields:
        return tuple(required_fields)
    return tuple(state.get("request", {}).get("required_fields", ()))


def _capability_claim_type(
    state: WorkflowState,
    capability_id: str,
) -> str:
    bound_inputs = state.get("request", {}).get("bound_capability_inputs") or {}
    bound = bound_inputs.get(capability_id) if isinstance(bound_inputs, Mapping) else None
    supported = tuple(
        str(item)
        for item in getattr(bound, "supported_claim_types", ()) or ()
        if str(item)
    )
    if not supported:
        registry = state.get("request", {}).get("runtime_registry")
        if not isinstance(registry, RuntimeContractRegistry):
            registry = RuntimeContractRegistry.from_path(
                CANONICAL_RUNTIME_BINDINGS_PATH
            )
        supported = tuple(
            str(item)
            for item in registry.capability_inputs(capability_id).get(
                "supported_claim_types", ()
            )
            if str(item)
        )
    requested = tuple(
        dict.fromkeys(
            str(item)
            for item in (
                *(
                    state.get("intent", {}).get(
                        "publishable_claim_types", ()
                    )
                ),
                *(
                    state.get("analysis_route", {})
                    .get("analysis_requirements", {})
                    .get("claim_types", ())
                ),
            )
            if str(item)
        )
    )
    return next(
        (claim_type for claim_type in requested if claim_type in supported),
        supported[0] if supported else "",
    )


def _capability_query_intents(capability_id: str) -> tuple[str, ...]:
    if capability_id in {"data_quality_profile", "data_quality_check"}:
        return ("data_quality_probe", "daily_metric_baselines", "clickhouse_revenue_rows")
    if capability_id == "high_value_user_contribution":
        return ("high_value_scan", "dimension_scan", "joint_candidate_scan", "daily_metric_baselines", "clickhouse_revenue_rows")
    if capability_id == "candidate_dimension_screen":
        return (
            "dimension_contribution_scan",
            "dimension_scan_reuse",
            "dimension_scan",
            "daily_metric_baselines",
        )
    if capability_id in {"segment_contribution", "segment_bridge", "user_mix_contribution"}:
        return ("dimension_scan_reuse", "dimension_scan", "joint_candidate_scan", "daily_metric_baselines", "clickhouse_revenue_rows")
    if capability_id == "joint_attribution":
        return ("joint_candidate_scan", "dimension_scan_reuse", "dimension_scan", "daily_metric_baselines", "clickhouse_revenue_rows")
    if capability_id == "event_evidence":
        return ("event_context_probe",)
    if capability_id in {
        "cross_source_association",
        "cross_source_panel_association",
    }:
        return (
            "association_outcome_timeseries",
            "association_candidate_timeseries",
        )
    if capability_id in {
        "compare_periods",
        "rolling_window_compare",
        "outlier_scan",
        "outlier_contribution",
    }:
        return ("daily_metric_baselines", "dimension_scan", "joint_candidate_scan", "clickhouse_revenue_rows")
    if capability_id == "driver_decomposition":
        return ("component_driver_scan", "daily_metric_baselines", "dimension_scan", "joint_candidate_scan", "clickhouse_revenue_rows")
    if capability_id == "pattern_scan":
        return ("time_bucket_scan", "daily_metric_baselines", "dimension_scan", "joint_candidate_scan", "clickhouse_revenue_rows")
    return ("clickhouse_revenue_rows", "daily_metric_baselines", "dimension_scan", "joint_candidate_scan")


COMPARISON_MEASURE_KEYS = frozenset(
    {
        "amount",
        "paid_amount",
        "paid_users",
        "orders",
        "paid_orders",
        "first_paid_users",
        "high_value_amount",
        "high_value_paid_users",
        "paid_frequency",
        "avg_order_amount",
        "first_pay_user_share",
        "payment_success_rate",
        "n",
        "sample_size",
        "order_count",
        "user_count",
    }
)


def _comparison_rows_and_params(
    state: WorkflowState,
    capability_id: str,
    *,
    params: Mapping[str, Any],
    dimension_keys: Sequence[str],
    period_key: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = [dict(row) for row in _capability_rows_for(state, capability_id)]
    output_params = dict(params)
    group_key = str(output_params.get("group_key", "group"))
    target_group = str(output_params.get("target_group", "target"))
    requested_baseline = str(output_params.get("baseline_group", "baseline"))
    projected_rows = _claim_scoped_window_rows(
        state,
        capability_id,
        rows,
        group_key=group_key,
        target_group=target_group,
        baseline_group=requested_baseline,
        period_key=period_key,
        dimension_keys=tuple(str(key) for key in dimension_keys if key),
    )
    if projected_rows is not None:
        output_params["baseline_group"] = requested_baseline
        return projected_rows, output_params
    baseline_group = _comparison_baseline_group(
        state,
        rows,
        group_key=group_key,
        target_group=target_group,
        requested_baseline=requested_baseline,
    )
    output_params["baseline_group"] = baseline_group
    if not _has_comparison_groups(
        rows,
        group_key=group_key,
        target_group=target_group,
        baseline_group=baseline_group,
    ):
        return rows, output_params

    dimensions = tuple(str(key) for key in dimension_keys if key)
    if period_key and _has_paired_comparison_rows(
        rows,
        group_key=group_key,
        target_group=target_group,
        baseline_group=baseline_group,
        period_key=str(period_key),
        dimension_keys=dimensions,
    ):
        return rows, output_params
    comparison_rows = _aggregate_comparison_rows(
        rows,
        group_key=group_key,
        target_group=target_group,
        baseline_group=baseline_group,
        dimension_keys=dimensions,
        period_key=period_key,
    )
    if not comparison_rows:
        return rows, output_params
    return comparison_rows, output_params


def _claim_scoped_window_rows(
    state: WorkflowState,
    capability_id: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    group_key: str,
    target_group: str,
    baseline_group: str,
    period_key: str | None,
    dimension_keys: tuple[str, ...],
) -> list[dict[str, Any]] | None:
    """Project a shared authoritative query onto one claim's exact window pair."""

    if not rows or not any(row.get("window_id") for row in rows):
        return None
    selected_baselines = _claim_scoped_baseline_ids(state, capability_id)
    if selected_baselines is None:
        return None
    if len(selected_baselines) != 1:
        return []
    baseline_window_id = selected_baselines[0]
    target_window_ids = tuple(
        dict.fromkeys(
            str(row.get("window_id") or "")
            for row in rows
            if str(row.get("window_role") or "") == "target"
            and str(row.get("window_id") or "")
        )
    )
    if len(target_window_ids) != 1:
        return []
    target_window_id = target_window_ids[0]
    selected_ids = (target_window_id, baseline_window_id)
    if any(
        not any(str(row.get("window_id") or "") == window_id for row in rows)
        for window_id in selected_ids
    ):
        return []

    window_specs = _comparison_window_specs(state)
    buckets: dict[tuple[str, tuple[Any, ...]], dict[str, dict[str, Any]]] = {}
    for row in rows:
        window_id = str(row.get("window_id") or "")
        if window_id not in selected_ids:
            continue
        expected_role = "target" if window_id == target_window_id else "baseline"
        if str(row.get("window_role") or "") != expected_role:
            raise WorkflowFailure(
                "comparison_window_projection_invalid:window_role",
                failure_type="contract",
            )
        dimensions = tuple(row.get(key) for key in dimension_keys)
        if any(value in (None, "") for value in dimensions):
            continue
        observation_key = str(row.get("observation_key") or "")
        if not observation_key:
            raise WorkflowFailure(
                "comparison_window_projection_invalid:observation_key",
                failure_type="contract",
            )
        bucket = buckets.setdefault((window_id, dimensions), {})
        merged = bucket.setdefault(observation_key, {})
        for key, value in row.items():
            if key in merged and merged[key] not in (None, value) and value is not None:
                raise WorkflowFailure(
                    "comparison_window_projection_invalid:row_collision",
                    failure_type="contract",
                )
            if value is not None:
                merged[key] = value

    dimension_sets = {
        window_id: {
            dimensions
            for candidate_window_id, dimensions in buckets
            if candidate_window_id == window_id
        }
        for window_id in selected_ids
    }
    paired_dimensions = dimension_sets[target_window_id].intersection(
        dimension_sets[baseline_window_id]
    )
    projected: list[dict[str, Any]] = []
    for dimensions in sorted(paired_dimensions, key=lambda value: repr(value)):
        for window_id, output_group in (
            (target_window_id, target_group),
            (baseline_window_id, baseline_group),
        ):
            observations = buckets.get((window_id, dimensions), {})
            aggregated = _aggregate_claim_window_rows(
                window_id,
                observations,
                window_specs.get(window_id),
            )
            if aggregated is None:
                continue
            projected.append(
                {
                    **{
                        key: value
                        for key, value in zip(dimension_keys, dimensions)
                    },
                    **aggregated,
                    "window_id": window_id,
                    "window_role": (
                        "target" if window_id == target_window_id else "baseline"
                    ),
                    "observation_key": f"aggregate:{window_id}",
                    group_key: output_group,
                    **({str(period_key): "comparison_window"} if period_key else {}),
                }
            )
    expected_rows = len(paired_dimensions) * 2
    return projected if len(projected) == expected_rows else []


def _comparison_group_window_id(
    rows: Sequence[Mapping[str, Any]],
    *,
    group_key: str,
    group_value: str,
) -> str:
    window_ids = tuple(
        dict.fromkeys(
            str(row.get("window_id") or "")
            for row in rows
            if str(row.get(group_key) or "") == group_value
            and str(row.get("window_id") or "")
        )
    )
    return window_ids[0] if len(window_ids) == 1 else ""


def _claim_scoped_baseline_ids(
    state: WorkflowState,
    capability_id: str,
) -> tuple[str, ...] | None:
    primary = tuple(
        str(window_id)
        for window_id in (
            (state.get("analysis_route") or {})
            .get("analysis_requirements", {})
            .get("baselines", ())
        )
        if str(window_id)
    )
    axis = next(
        (
            item
            for item in (state.get("intent") or {}).get("analysis_axes", ())
            if isinstance(item, Mapping)
            and capability_id in set(item.get("capability_refs") or ())
        ),
        {},
    )
    if str(axis.get("axis_id") or "") != "time_context":
        return tuple(dict.fromkeys(primary))
    registry = (state.get("request") or {}).get("runtime_registry")
    if not isinstance(registry, RuntimeContractRegistry):
        registry = RuntimeContractRegistry.from_path(
            CANONICAL_RUNTIME_BINDINGS_PATH
        )
    try:
        required_windows = registry.capability_inputs(capability_id).get(
            "required_windows", ()
        )
    except KeyError:
        return tuple(dict.fromkeys(primary))
    auxiliary = tuple(
        str(window_id)
        for window_id in required_windows
        if str(window_id) and str(window_id) != "target_day"
    )
    return tuple(dict.fromkeys(auxiliary or primary))


def _comparison_window_specs(
    state: WorkflowState,
) -> dict[str, Mapping[str, Any]]:
    raw = state.get("request", {}).get("analysis_contract")
    if raw is None:
        result = state.get("analysis_runtime_result")
        raw = getattr(result, "analysis_contract", None)
    if raw is None:
        return {}
    if hasattr(raw, "resolved_windows"):
        return {
            str(window.window_id): window.to_dict()
            for window in raw.resolved_windows
        }
    if isinstance(raw, Mapping):
        try:
            contract = analysis_contract_from_dict(raw)
        except (KeyError, TypeError, ValueError):
            return {}
        return {
            str(window.window_id): window.to_dict()
            for window in contract.resolved_windows
        }
    return {}


def _aggregate_claim_window_rows(
    window_id: str,
    observations: Mapping[str, Mapping[str, Any]],
    window_spec: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    keys = tuple(sorted(str(key) for key in observations))
    if window_spec:
        try:
            start = date.fromisoformat(str(window_spec.get("start_inclusive") or ""))
            end = date.fromisoformat(str(window_spec.get("end_exclusive") or ""))
            expected = tuple(
                (start + timedelta(days=offset)).isoformat()
                for offset in range((end - start).days)
            )
            required_days = int(window_spec.get("required_complete_days") or 0)
            aggregation = str(window_spec.get("aggregation") or "")
        except (TypeError, ValueError):
            return None
        if keys != expected or len(keys) != required_days:
            return None
    else:
        required_days = 7 if window_id == "rolling_7_day_baseline" else 1
        aggregation = (
            "mean_of_complete_days"
            if window_id == "rolling_7_day_baseline"
            else "daily_total"
        )
        if len(keys) != required_days:
            return None
    if aggregation not in {"daily_total", "mean_of_complete_days"}:
        return None

    rows = tuple(observations[key] for key in keys)
    numeric_keys = tuple(
        sorted(
            {
                str(key)
                for row in rows
                for key in row
                if key in COMPARISON_MEASURE_KEYS
            }
        )
    )
    result: dict[str, Any] = {}
    for key in numeric_keys:
        values = tuple(_as_float(row.get(key)) for row in rows)
        if any(value is None for value in values):
            continue
        result[key] = (
            values[0]
            if aggregation == "daily_total"
            else sum(value for value in values if value is not None) / required_days
        )

    paid_amount = _as_float(result.get("paid_amount"))
    if paid_amount is None:
        paid_amount = _as_float(result.get("amount"))
    paid_orders = _as_float(result.get("paid_orders"))
    if paid_orders is None:
        paid_orders = _as_float(result.get("orders"))
    paid_users = _as_float(result.get("paid_users"))
    first_paid_users = _as_float(result.get("first_paid_users"))
    if paid_amount is not None:
        result["paid_amount"] = paid_amount
        result["amount"] = paid_amount
    if paid_orders is not None:
        result["paid_orders"] = paid_orders
        result["orders"] = paid_orders
    if paid_orders is not None and paid_users:
        result["paid_frequency"] = paid_orders / paid_users
    if paid_amount is not None and paid_orders:
        result["avg_order_amount"] = paid_amount / paid_orders
    if first_paid_users is not None and paid_users:
        result["first_pay_user_share"] = first_paid_users / paid_users
    return result


def _comparison_baseline_group(
    state: WorkflowState,
    rows: Sequence[Mapping[str, Any]],
    *,
    group_key: str,
    target_group: str,
    requested_baseline: str,
) -> str:
    groups = {
        str(row.get(group_key))
        for row in rows
        if row.get(group_key) not in (None, "")
    }
    candidates = [requested_baseline]
    runtime_plan = state.get("request", {}).get("compiler_runtime_plan") or {}
    if isinstance(runtime_plan, Mapping):
        baselines = runtime_plan.get("baselines") or ()
        if isinstance(baselines, Sequence) and not isinstance(baselines, (str, bytes)):
            candidates.extend(str(item) for item in baselines if item)
        capability_params = runtime_plan.get("capability_params") or {}
        if isinstance(capability_params, Mapping):
            compare_params = capability_params.get("compare_periods") or {}
            if isinstance(compare_params, Mapping):
                compare_baselines = compare_params.get("baselines") or ()
                if isinstance(compare_baselines, Sequence) and not isinstance(
                    compare_baselines,
                    (str, bytes),
                ):
                    candidates.extend(str(item) for item in compare_baselines if item)
    candidates.extend(
        (
            "baseline",
            "previous_day",
            "same_weekday_last_week",
            "rolling_7_day_baseline",
            "custom_baseline",
            "history",
        )
    )
    for candidate in dict.fromkeys(candidates):
        if candidate and candidate != target_group and candidate in groups:
            return candidate
    return requested_baseline


def _has_comparison_groups(
    rows: Sequence[Mapping[str, Any]],
    *,
    group_key: str,
    target_group: str,
    baseline_group: str,
) -> bool:
    groups = {
        str(row.get(group_key))
        for row in rows
        if row.get(group_key) not in (None, "")
    }
    return target_group in groups and baseline_group in groups


def _has_paired_comparison_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    group_key: str,
    target_group: str,
    baseline_group: str,
    period_key: str,
    dimension_keys: tuple[str, ...],
) -> bool:
    buckets: dict[tuple[Any, ...], set[str]] = {}
    for row in rows:
        period = row.get(period_key)
        group = row.get(group_key)
        if period in (None, "") or group in (None, ""):
            continue
        dimension_values = tuple(row.get(key) for key in dimension_keys)
        if any(value in (None, "") for value in dimension_values):
            continue
        key = (period, *dimension_values)
        buckets.setdefault(key, set()).add(str(group))
    return any({target_group, baseline_group}.issubset(groups) for groups in buckets.values())


def _aggregate_comparison_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    group_key: str,
    target_group: str,
    baseline_group: str,
    dimension_keys: tuple[str, ...],
    period_key: str | None,
) -> list[dict[str, Any]]:
    selected_groups = {target_group, baseline_group}
    buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
    counts: dict[tuple[Any, ...], int] = {}
    for row in rows:
        group = row.get(group_key)
        if str(group) not in selected_groups:
            continue
        dimension_values = tuple(row.get(key) for key in dimension_keys)
        if any(value in (None, "") for value in dimension_values):
            continue
        bucket_key = dimension_values + (str(group),)
        bucket = buckets.setdefault(
            bucket_key,
            {
                **{key: value for key, value in zip(dimension_keys, dimension_values)},
                group_key: str(group),
            },
        )
        if period_key:
            bucket[period_key] = "comparison_window"
        counts[bucket_key] = counts.get(bucket_key, 0) + 1
        for measure in COMPARISON_MEASURE_KEYS:
            value = _as_float(row.get(measure))
            if value is None:
                continue
            bucket[measure] = _as_float(bucket.get(measure)) or 0.0
            bucket[measure] += value

    for key, bucket in buckets.items():
        if bucket.get(group_key) != "rolling_7_day_baseline":
            continue
        count = counts.get(key) or 1
        for measure in COMPARISON_MEASURE_KEYS:
            if measure in bucket:
                bucket[measure] = bucket[measure] / count
    return list(buckets.values())


def _runtime_dimension_keys_for_intents(
    state: WorkflowState,
    intents: Sequence[str],
) -> tuple[str, ...]:
    row_query_plan = state.get("row_query_plan") or {}
    candidates = []
    if isinstance(row_query_plan, Mapping):
        for key in ("query_results", "query_plans"):
            values = row_query_plan.get(key) or ()
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                candidates.extend(item for item in values if isinstance(item, Mapping))
    for intent in intents:
        for item in candidates:
            item_intent = item.get("intent") or item.get("query_intent")
            if str(item_intent or "") != intent:
                continue
            dimensions = item.get("dimension_keys") or ()
            keys: tuple[str, ...] = ()
            if isinstance(dimensions, Sequence) and not isinstance(dimensions, (str, bytes)):
                keys = tuple(str(key) for key in dimensions if key)
            if keys:
                return keys
    rows_by_intent = state.get("request", {}).get("runtime_rows_by_intent") or {}
    if isinstance(rows_by_intent, Mapping):
        for intent in intents:
            rows = rows_by_intent.get(intent)
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                keys = _infer_runtime_dimension_keys(rows)
                if keys:
                    return keys
    return ()


def _infer_runtime_dimension_keys(rows: Sequence[Any]) -> tuple[str, ...]:
    excluded = {
        "period",
        "group",
        "amount",
        "paid_users",
        "orders",
        "first_paid_users",
        "high_value_amount",
        "high_value_paid_users",
        "paid_frequency",
        "avg_order_amount",
        "first_pay_user_share",
        "payment_success_rate",
        "n",
        "sample_size",
        "order_count",
        "user_count",
        "min_period",
        "max_period",
    }
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        keys = [
            str(key)
            for key, value in row.items()
            if key not in excluded and value not in (None, "")
        ]
        if keys:
            return tuple(keys)
    return ()


def _driver_params(state: WorkflowState) -> dict[str, Any]:
    params = dict(state.get("intent", {}).get("pattern_params", {}))
    return {
        "period_key": params.get("period_key", "period"),
        "group_key": params.get("group_key", "group"),
        "target_group": params.get("target_group", "target"),
        "baseline_group": params.get("baseline_group", "baseline"),
    }


def _candidate_dimension_screen_params(
    state: WorkflowState,
    *,
    prior_evidence: Sequence[Any] = (),
) -> dict[str, Any]:
    params = dict(state.get("intent", {}).get("pattern_params", {}))
    group_key = str(params.get("group_key") or "group")
    target_group = str(params.get("target_group") or "target")
    baseline_group = str(params.get("baseline_group") or "baseline")
    requested_dimensions = tuple(
        dict.fromkeys(
            str(dimension)
            for dimension in (
                (state.get("analysis_route") or {})
                .get("analysis_requirements", {})
                .get("dimension_ids", ())
            )
            if str(dimension)
        )
    )
    raw_rows = tuple(
        dict(row)
        for row in _capability_rows_for(state, "candidate_dimension_screen")
        if isinstance(row, Mapping)
    )
    rows_by_dimension = {
        dimension: _project_sparse_dimension_comparison_rows(
            state,
            raw_rows,
            dimension=dimension,
            group_key=group_key,
            target_group=target_group,
            baseline_group=baseline_group,
        )
        for dimension in requested_dimensions
    }

    driver_params = _driver_params(state)
    driver_rows, driver_params = _comparison_rows_and_params(
        state,
        "driver_decomposition",
        params=driver_params,
        dimension_keys=(),
        period_key=None,
    )
    overall_by_group: dict[str, float] = {}
    for row in driver_rows:
        group = str(row.get(group_key) or "")
        amount = _as_float(row.get("paid_amount"))
        if amount is None:
            amount = _as_float(row.get("amount"))
        if group in {target_group, baseline_group} and amount is not None:
            overall_by_group[group] = amount

    registry = state.get("request", {}).get("runtime_registry")
    if not isinstance(registry, RuntimeContractRegistry):
        registry = RuntimeContractRegistry.from_path(
            CANONICAL_RUNTIME_BINDINGS_PATH
        )
    dimension_labels = {}
    dimension_metadata = {}
    for dimension in requested_dimensions:
        try:
            binding = registry.dimension(dimension)
        except KeyError:
            continue
        label = str(binding.get("business_name") or "")
        if label:
            dimension_labels[dimension] = label
        dimension_metadata[dimension] = {
            key: binding[key]
            for key in (
                "business_name",
                "hierarchy_id",
                "hierarchy_level",
                "parent_dimension",
            )
            if binding.get(key) not in (None, "")
        }

    bound, limitation = _production_bound_input(
        state,
        "candidate_dimension_screen",
    )
    available_dimensions = tuple(
        dimension for dimension, rows in rows_by_dimension.items() if rows
    )
    complete_dimensions = (
        available_dimensions
        if not limitation and (bound is None or bound.status == "ready")
        else available_dimensions
        if not limitation and bound is not None and bound.status == "degraded"
        else ()
    )
    global_primary_factor = ""
    for item in reversed(tuple(prior_evidence)):
        capability = (
            str(item.get("capability") or item.get("capability_id") or "")
            if isinstance(item, Mapping)
            else str(getattr(item, "capability", "") or "")
        )
        if capability != "driver_decomposition":
            continue
        payload = (
            item.get("typed_payload") or {}
            if isinstance(item, Mapping)
            else getattr(item, "typed_payload", {}) or {}
        )
        if isinstance(payload, Mapping):
            global_primary_factor = str(
                payload.get("primary_core_driver") or ""
            )
        break
    return {
        "rows_by_dimension": rows_by_dimension,
        "overall_by_group": overall_by_group,
        "complete_dimensions": complete_dimensions,
        "dimension_labels": dimension_labels,
        "dimension_metadata": dimension_metadata,
        "global_primary_factor": global_primary_factor,
        "group_key": group_key,
        "target_group": target_group,
        "baseline_group": baseline_group,
        "amount_key": "amount",
        "min_sample_size": int(params.get("dimension_min_sample_size") or 10),
        "top_k": int(params.get("dimension_top_k") or 5),
        "result_refs": _capability_result_refs_for(
            state,
            "candidate_dimension_screen",
        ),
    }


def _project_sparse_dimension_comparison_rows(
    state: WorkflowState,
    rows: Sequence[Mapping[str, Any]],
    *,
    dimension: str,
    group_key: str,
    target_group: str,
    baseline_group: str,
) -> tuple[dict[str, Any], ...]:
    selected_baselines = _claim_scoped_baseline_ids(
        state,
        "candidate_dimension_screen",
    )
    if selected_baselines is None or len(selected_baselines) != 1:
        return ()
    baseline_window_id = selected_baselines[0]
    target_window_ids = tuple(
        dict.fromkeys(
            str(row.get("window_id") or "")
            for row in rows
            if dimension in row
            and str(row.get("window_role") or "") == "target"
            and str(row.get("window_id") or "")
        )
    )
    if len(target_window_ids) != 1:
        return ()
    target_window_id = target_window_ids[0]
    selected_ids = (target_window_id, baseline_window_id)
    window_specs = _comparison_window_specs(state)
    observations: dict[
        tuple[str, str], dict[str, dict[str, Any]]
    ] = {}
    measure_keys: set[str] = set()
    values: set[str] = set()
    window_observation_keys: dict[str, set[str]] = {
        window_id: set() for window_id in selected_ids
    }
    for row in rows:
        if dimension not in row:
            continue
        window_id = str(row.get("window_id") or "")
        if window_id not in selected_ids:
            continue
        expected_role = "target" if window_id == target_window_id else "baseline"
        if str(row.get("window_role") or "") != expected_role:
            raise WorkflowFailure(
                "candidate_dimension_projection_invalid:window_role",
                failure_type="contract",
            )
        observation_key = str(row.get("observation_key") or "")
        if not observation_key:
            raise WorkflowFailure(
                "candidate_dimension_projection_invalid:observation_key",
                failure_type="contract",
            )
        raw_value = row.get(dimension)
        value = str(raw_value).strip() if raw_value not in (None, "") else "Unknown"
        values.add(value)
        window_observation_keys[window_id].add(observation_key)
        merged = observations.setdefault((window_id, value), {}).setdefault(
            observation_key,
            {},
        )
        for key, item in row.items():
            if key in COMPARISON_MEASURE_KEYS and item is not None:
                if key in merged and merged[key] != item:
                    raise WorkflowFailure(
                        "candidate_dimension_projection_invalid:row_collision",
                        failure_type="contract",
                    )
                merged[key] = item
                measure_keys.add(str(key))

    projected: list[dict[str, Any]] = []
    for value in sorted(values):
        for window_id, group in (
            (target_window_id, target_group),
            (baseline_window_id, baseline_group),
        ):
            current = {
                key: dict(item)
                for key, item in observations.get((window_id, value), {}).items()
            }
            expected_keys = _candidate_dimension_observation_keys(
                window_id,
                window_specs.get(window_id),
                tuple(sorted(window_observation_keys[window_id])),
            )
            if not expected_keys:
                return ()
            missing_entire_window = not current
            if missing_entire_window:
                continue
            for observation_key in expected_keys:
                current.setdefault(
                    observation_key,
                    {key: 0.0 for key in measure_keys},
                )
            aggregated = _aggregate_claim_window_rows(
                window_id,
                current,
                window_specs.get(window_id),
            )
            if aggregated is None:
                return ()
            projected.append(
                {
                    dimension: value,
                    **aggregated,
                    "window_id": window_id,
                    "window_role": (
                        "target" if window_id == target_window_id else "baseline"
                    ),
                    "observation_key": f"aggregate:{window_id}",
                    group_key: group,
                }
            )
    return tuple(projected)


def _candidate_dimension_observation_keys(
    window_id: str,
    window_spec: Mapping[str, Any] | None,
    observed: tuple[str, ...],
) -> tuple[str, ...]:
    if window_spec:
        try:
            start = date.fromisoformat(str(window_spec.get("start_inclusive") or ""))
            end = date.fromisoformat(str(window_spec.get("end_exclusive") or ""))
        except ValueError:
            return ()
        return tuple(
            (start + timedelta(days=offset)).isoformat()
            for offset in range((end - start).days)
        )
    required_days = 7 if window_id == "rolling_7_day_baseline" else 1
    return observed if len(observed) == required_days else ()


def _segment_contribution_params(state: WorkflowState) -> dict[str, Any]:
    params = dict(state.get("intent", {}).get("pattern_params", {}))
    runtime_dimensions = _runtime_dimension_keys_for_intents(
        state,
        ("dimension_scan_reuse", "dimension_scan", "joint_candidate_scan"),
    )
    return {
        "segment_key": params.get("segment_key") or (runtime_dimensions[0] if runtime_dimensions else params.get("period_key", "period")),
        "group_key": params.get("group_key", "group"),
        "target_group": params.get("target_group", "target"),
        "baseline_group": params.get("baseline_group", "baseline"),
    }


def _joint_attribution_params(state: WorkflowState) -> dict[str, Any]:
    params = dict(state.get("intent", {}).get("pattern_params", {}))
    has_explicit_dimensions = bool(
        state["request"].get("joint_dimension_keys")
        or params.get("joint_dimension_keys")
        or params.get("dimension_keys")
    )
    dimensions = (
        state["request"].get("joint_dimension_keys")
        or params.get("joint_dimension_keys")
        or params.get("dimension_keys")
        or _infer_runtime_dimension_keys(
            _capability_rows_for(state, "joint_attribution")
        )
        or ()
    )
    if isinstance(dimensions, str):
        dimensions = tuple(part.strip() for part in dimensions.split(",") if part.strip())
    bound, limitation = _production_bound_input(state, "joint_attribution")
    return {
        "dimension_keys": tuple(dimensions),
        "group_key": params.get("group_key", "group"),
        "target_group": params.get("target_group", "target"),
        "baseline_group": params.get("baseline_group", "baseline"),
        "amount_key": params.get("amount_key", "amount"),
        "force_run": has_explicit_dimensions or (bound is not None and not limitation),
    }


def _outlier_contribution_params(state: WorkflowState) -> dict[str, Any]:
    params = dict(state.get("intent", {}).get("pattern_params", {}))
    return {
        "period_key": params.get("period_key", "period"),
        "period_grain": params.get("period_grain", state["intent"].get("grain", "period")),
        "group_key": params.get("group_key", "group"),
        "target_group": params.get("target_group", "target"),
        "baseline_group": params.get("baseline_group", "baseline"),
        "removal_policy": params.get(
            "removal_policy", "top_positive_contribution_periods"
        ),
        "max_removed_periods": params.get("max_removed_periods", 5),
        "direction_after_removal": params.get("direction_after_removal", True),
    }


def _user_mix_contribution_params(state: WorkflowState) -> dict[str, Any]:
    params = dict(state.get("intent", {}).get("pattern_params", {}))
    return {
        "segment_key": params.get("segment_key", "channel"),
        "user_grain_policy": params.get("user_grain_policy", "new_vs_returning"),
        "mix_key": params.get("mix_key", "user_mix_bucket"),
        "group_key": params.get("group_key", "group"),
        "amount_key": params.get("amount_key", "amount"),
        "users_key": params.get("users_key", "paid_users"),
    }


def _high_value_user_contribution_params(state: WorkflowState) -> dict[str, Any]:
    params = dict(state.get("intent", {}).get("pattern_params", {}))
    return {
        "threshold_policy": params.get("threshold_policy"),
        "group_key": params.get("group_key", "group"),
        "amount_key": params.get("amount_key", "amount"),
        "users_key": params.get("users_key", "paid_users"),
    }


def _event_evidence_params(state: WorkflowState) -> dict[str, Any]:
    params = dict(state.get("intent", {}).get("pattern_params", {}))
    return {
        "event_window_policy": params.get("event_window_policy")
        or state["request"].get("event_window_policy"),
        "low_risk_default": params.get(
            "low_risk_default",
            state["request"].get("low_risk_default", True),
        ),
    }


def _decide_next_action(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "decide_next_action")
    payload = {
        "intent": state["intent"],
        "accepted_graph": to_jsonable(
            state["compiled_graph"].mutations.accepted_graph
        ),
        "evidence_brief": state["evidence_brief"],
        "diagnostic_insights": to_jsonable(
            state.get("diagnostic_insights") or {}
        ),
        "allow_question_interrupt": state["request"].get(
            "allow_question_interrupt", True
        ),
    }
    try:
        provider_output = _invoke_llm(
            state,
            "next_action",
            payload,
            output_validator=_validate_next_action_provider_output,
            defer_narrative_validation=True,
        )
        _validate_next_action_provider_output(provider_output)
        state["next_action"] = {
            **provider_output,
            **_next_action_business_narrative(
                state,
                action=str(provider_output["next_action"]),
            ),
            "provider_narrative_audit_only": True,
        }
    except (WorkflowFailure, TypeError, ValueError) as exc:
        fallback = _deterministic_next_action(
            state,
            fallback_reason=_exception_reason(exc),
        )
        state["next_action"] = fallback
        state["llm_calls"].append(
            _local_llm_decision_audit(
                task="next_action",
                payload=payload,
                output=fallback,
                reason="next_action_narrative_provider_fallback",
            )
        )
    return state


_NEXT_ACTION_VALUES = frozenset(
    {
        "continue_evidence",
        "scan_sibling",
        "promote_attribution",
        "ask_question",
        "synthesize_answer",
        "degrade",
    }
)


def _validate_next_action_provider_output(output: Mapping[str, Any]) -> None:
    action = output.get("next_action")
    if action not in _NEXT_ACTION_VALUES:
        raise ValueError("next_action_invalid")
    for field in ("decision_summary", "display_summary"):
        value = output.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"next_action_narrative_shape_invalid:{field}")


def _next_action_business_narrative(
    state: WorkflowState,
    *,
    action: str,
) -> dict[str, str]:
    if action in {"continue_evidence", "scan_sibling"}:
        summary = "仍有能够改变主要判断的分析路径，继续补充相关证据。"
    elif action == "promote_attribution":
        summary = "现有结果支持继续检查更细的贡献归属，下一步补充对应证据。"
    elif action == "ask_question":
        summary = "仍有一项会改变业务结论的选择，需要先由用户确认。"
    elif action == "degrade":
        summary = "当前证据还不能支持主要业务结论，将保留已经确认的事实和缺口。"
    else:
        summary = (
            "已验证的核心比较和贡献证据可以形成答案；"
            "未完成的辅助分析将在答案中单独说明。"
        )
    return {
        "decision_summary": summary,
        "display_summary": summary,
    }


def _deterministic_next_action(
    state: WorkflowState,
    *,
    fallback_reason: str,
) -> dict[str, Any]:
    pending_routes = _pending_diagnostic_route_ids(state)
    sufficiency = (
        (state.get("diagnostic_insights") or {}).get("diagnostic_sufficiency")
        or {}
    )
    diagnostic_status = str(
        sufficiency.get("status") or sufficiency.get("decision") or ""
    )
    if diagnostic_status == "continue" and pending_routes:
        action = "continue_evidence"
        output = {
            "next_action": action,
            "diagnostic_route": pending_routes[0],
        }
    elif (
        _evidence_supports_bounded_answer(state)
        or _pattern_has_negative_answer_evidence(state)
    ):
        action = "synthesize_answer"
        output = {"next_action": action}
    else:
        action = "degrade"
        output = {"next_action": action}

    if action == "degrade" and _evidence_has_terminal_business_boundary(state):
        narrative = {
            "decision_summary": (
                "本地证据检查确认存在硬边界，当前不能发布主要业务结论；"
                "答案只保留已经验证的事实和缺口。"
            ),
            "display_summary": (
                "当前存在会阻断主要结论的证据边界，本轮只说明已验证事实和缺口。"
            ),
        }
    else:
        narrative = _next_action_business_narrative(state, action=action)

    return {
        **output,
        **narrative,
        "local_narrative_fallback": True,
        "fallback_reason": fallback_reason,
    }


def _promotion_direction(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "promotion_direction")
    output = _invoke_llm(
        state,
        "promotion_direction",
        {"intent": state["intent"], "evidence_brief": state["evidence_brief"]},
    )
    state["analysis_route"] = {**state.get("analysis_route", {}), "promotion": output}
    return state


def _promotion_policy_gate(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "promotion_policy_gate")
    requested = _requested_node_ids(
        state.get("analysis_route", {}).get("promotion", {}).get("requested_nodes", ())
    )
    if "joint_attribution" in requested:
        _current_event(state)["route"] = "accepted"
    else:
        _current_event(state)["route"] = "degraded_or_skip"
    return state


def _route_capability_cards(
    *,
    include_blocked: bool = False,
    registry: RuntimeContractRegistry | None = None,
) -> list[dict[str, Any]]:
    registry = registry or RuntimeContractRegistry.from_path(
        CANONICAL_RUNTIME_BINDINGS_PATH
    )
    cards = []
    for card in llm_capability_cards():
        capability_id = str(card.get("capability_id") or "")
        if (
            capability_id in ROUTE_BLOCKED_CAPABILITY_IDS
            and not include_blocked
        ):
            continue
        try:
            binding = registry.capability_inputs(capability_id)
        except KeyError:
            binding = {}
        cards.append(
            {
                **card,
                "supported_claim_types": list(
                    binding.get("supported_claim_types") or ()
                ),
                "allowed_claim_types": list(
                    binding.get("supported_claim_types") or ()
                ),
                "runtime_input_contract": {
                    key: binding[key]
                    for key in (
                        "metric_mode",
                        "required_metrics",
                        "optional_metrics",
                        "allowed_metrics",
                        "allowed_datasets",
                        "source_mode",
                        "query_families",
                        "supported_claim_types",
                        "context_window_policy",
                    )
                    if key in binding
                },
            }
        )
    return cards


def _normalize_route_requested_nodes(
    nodes: tuple[str, ...],
    intent: Mapping[str, Any],
) -> tuple[str, ...]:
    compiled = compile_graph(
        question_family=str(intent.get("question_family") or "pattern_explanation"),
        target_metric=str(intent.get("target_metric") or "paid_amount"),
        pattern_family=str(intent.get("pattern_family") or "intra_period"),
        requested_nodes=nodes,
        question_families=tuple(_intent_question_family_set(intent)),
        question_text=_intent_business_text(intent),
        bound_context=dict(intent),
    )
    return compiled.mutations.accepted_graph


def _compiler_bound_context(state: WorkflowState) -> dict[str, Any]:
    intent = dict(state.get("intent") or {})
    analysis_route = state.get("analysis_route") or {}
    request = state.get("request") or {}
    context = {
        key: intent.get(key)
        for key in (
            "pattern_family",
            "pattern_params",
            "time_window",
            "baseline",
            "target",
            "scope",
            "component_ids",
            "dimension_ids",
        )
        if intent.get(key) not in ("", None, {}, [])
    }
    route_requirements = analysis_route.get("analysis_requirements")
    if isinstance(route_requirements, Mapping):
        context["analysis_requirements"] = dict(route_requirements)
    analysis_contract = (
        state.get("analysis_contract")
        or request.get("analysis_contract")
    )
    outcome = state.get("analysis_compile_outcome") or request.get(
        "analysis_compile_outcome"
    )
    if analysis_contract is None and outcome is not None:
        analysis_contract = getattr(outcome, "analysis_contract", None)
    if hasattr(analysis_contract, "to_dict"):
        analysis_contract = analysis_contract.to_dict()
    if isinstance(analysis_contract, Mapping):
        normalized_analysis = dict(analysis_contract)
        context["analysis_contract"] = normalized_analysis
        if normalized_analysis.get("as_of") not in (None, ""):
            context["as_of"] = str(normalized_analysis["as_of"])
        resolved = normalized_analysis.get("resolved_windows")
        if isinstance(resolved, Sequence) and not isinstance(
            resolved, (str, bytes)
        ):
            context["resolved_windows"] = tuple(
                dict(item) for item in resolved if isinstance(item, Mapping)
            )
    elif request.get("as_of") not in (None, ""):
        context["as_of"] = str(request.get("as_of"))
    if isinstance(request.get("contract_versions"), Mapping):
        context["contract_versions"] = {
            str(key): str(value)
            for key, value in request["contract_versions"].items()
            if key not in ("", None) and value not in ("", None)
        }
    elif request.get("contract_version") not in ("", None):
        context["contract_versions"] = {"runtime": str(request.get("contract_version"))}
    if request.get("schema_fingerprint") not in ("", None):
        context["schema_fingerprint"] = str(request.get("schema_fingerprint"))
    manifest = request.get("context_manifest")
    if isinstance(manifest, Mapping):
        snapshot_version = manifest.get("snapshot_version")
        if snapshot_version not in ("", None):
            context["snapshot_version"] = str(snapshot_version)
        contract_versions = manifest.get("contract_versions")
        if isinstance(contract_versions, Mapping):
            context["contract_versions"] = {
                str(key): str(value)
                for key, value in contract_versions.items()
                if key not in ("", None) and value not in ("", None)
            }
        schema_fingerprint = manifest.get("schema_fingerprint")
        if schema_fingerprint not in ("", None):
            context["schema_fingerprint"] = str(schema_fingerprint)
    accepted_choice = request.get("accepted_degradation_choice") or {}
    if isinstance(accepted_choice, Mapping) and accepted_choice:
        versions = dict(context.get("contract_versions") or {})
        assumption_digest = material_assumption_digest(accepted_choice)
        if assumption_digest:
            versions["accepted_degradation_choice"] = assumption_digest
        context["contract_versions"] = versions
        context["accepted_degradation_choice"] = dict(accepted_choice)
    if isinstance(request.get("runtime_windows"), dict):
        context["windows"] = dict(request["runtime_windows"])
    elif isinstance(analysis_route.get("windows"), dict):
        context["windows"] = dict(analysis_route["windows"])
    for schema_key in ("schema_fields", "clickhouse_schema_fields"):
        values = request.get(schema_key)
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
            context[schema_key] = tuple(str(value) for value in values if value)
    if "schema_fields" not in context and "clickhouse_schema_fields" not in context:
        provider = _runtime_row_provider(request)
        if provider is not None and hasattr(provider, "schema_fields"):
            fields = provider.schema_fields()
            if isinstance(fields, Sequence) and not isinstance(fields, (str, bytes)):
                context["clickhouse_schema_fields"] = tuple(str(value) for value in fields if value)
    if request.get("runtime_baselines"):
        context["baselines"] = tuple(request["runtime_baselines"])
    elif analysis_route.get("baselines"):
        context["baselines"] = tuple(analysis_route["baselines"])
    for authority_key in (
        "evidence_resolver",
        "release_resolver",
        "rows_loader",
        "runtime_registry",
    ):
        if request.get(authority_key) is not None:
            context[authority_key] = request[authority_key]
    return context


def _reused_dimension_scan_input(state: WorkflowState) -> Optional[Mapping[str, Any]]:
    request = state.get("request") or {}
    plan = request.get("compiler_runtime_plan")
    if not isinstance(plan, Mapping):
        return None
    asset_rows = plan.get("asset_row_inputs")
    if not isinstance(asset_rows, Sequence) or isinstance(asset_rows, (str, bytes)):
        return None
    for item in asset_rows:
        if isinstance(item, Mapping) and item.get("query_ref") and item.get("rows"):
            return item
    return None


def _apply_reused_dimension_scan_input(
    state: WorkflowState,
    asset_input: Mapping[str, Any],
    *,
    additional_result_refs: Sequence[str] = (),
) -> None:
    request = state.setdefault("request", {})
    rows = [
        dict(row)
        for row in (asset_input.get("rows") or ())
        if isinstance(row, Mapping)
    ]
    result_refs = [
        str(ref)
        for ref in (
            *(asset_input.get("result_refs") or ()),
            *tuple(additional_result_refs or ()),
        )
        if ref
    ]
    if not result_refs and asset_input.get("query_ref"):
        result_refs = [str(asset_input["query_ref"])]
    request["rows"] = rows
    request["runtime_rows_source"] = "analysis_asset"
    request["result_refs"] = tuple(dict.fromkeys(result_refs))
    request["joint_dimension_keys"] = tuple(asset_input.get("dimensions") or ())
    existing_request_rows = request.get("runtime_rows_by_intent")
    if not isinstance(existing_request_rows, Mapping):
        existing_request_rows = {}
    request["runtime_rows_by_intent"] = {
        **{
            str(intent): [dict(row) for row in rows_]
            for intent, rows_ in dict(existing_request_rows).items()
        },
        "dimension_scan_reuse": rows,
    }
    existing_request_refs = request.get("result_refs_by_intent")
    if not isinstance(existing_request_refs, Mapping):
        existing_request_refs = {}
    request["result_refs_by_intent"] = {
        **{
            str(intent): list(refs)
            for intent, refs in dict(existing_request_refs).items()
        },
        "dimension_scan_reuse": list(dict.fromkeys(result_refs)),
    }

    row_query_plan = state.setdefault("row_query_plan", {})
    row_query_plan["query_intent"] = "dimension_scan_reuse"
    row_query_plan["query_ref"] = str(asset_input.get("query_ref") or "")
    row_query_plan["result_refs"] = list(dict.fromkeys(result_refs))
    row_query_plan["reused_asset_ref"] = str(asset_input.get("query_ref") or "")
    row_query_plan["dimension_keys"] = list(asset_input.get("dimensions") or ())
    row_query_plan["rows"] = rows
    existing_rows = row_query_plan.get("rows_by_intent")
    if not isinstance(existing_rows, Mapping):
        existing_rows = {}
    row_query_plan["rows_by_intent"] = {
        **{
            str(intent): [dict(row) for row in rows_]
            for intent, rows_ in dict(existing_rows).items()
        },
        "dimension_scan_reuse": rows,
    }


def _intent_business_text(intent: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            "target_claim": intent.get("target_claim"),
            "question": intent.get("question"),
            "sub_intents": intent.get("sub_intents"),
            "baseline": intent.get("baseline"),
            "target": intent.get("target"),
        },
        ensure_ascii=False,
        default=str,
    ).lower()


def _contains_segment_dimension(text: str) -> bool:
    return _contains_any(
        text,
        ("渠道", "分群", "segment", "channel", "拖累", "结构", "组合", "分布"),
    )


def _business_text_requests_segment_contribution(text: str) -> bool:
    if not _contains_segment_dimension(text):
        return False
    return _contains_any(
        text,
        ("哪些", "各", "贡献", "解释", "拉动", "拖累", "归因", "分解", "变化"),
    )


def _business_text_requests_change_explanation(text: str) -> bool:
    return _contains_any(
        text,
        ("提升", "增长", "下降", "减少", "变化", "差异", "q2", "q1", "环比", "同比"),
    ) and _contains_any(
        text,
        ("原因", "为什么", "驱动", "影响因子", "贡献", "解释", "归因", "分解"),
    )


def _business_text_requests_joint_attribution(text: str) -> bool:
    if not _contains_segment_dimension(text):
        return False
    return _contains_any(
        text,
        ("主要原因", "原因", "贡献最大", "最明显", "解释", "这些渠道", "渠道里", "组合", "共同"),
    )


def _business_text_requests_outlier_recalc(text: str) -> bool:
    return (
        _contains_any(text, ("移除", "剔除", "排除", "去掉", "排掉"))
        and _contains_any(text, ("按日", "按天", "日期", "天", "日", "异常"))
        and _contains_any(text, ("复算", "贡献最大", "最大正向", "方向", "成立"))
    )


def _business_text_requests_period_recompare(text: str) -> bool:
    return _contains_any(
        text,
        ("日均", "日平均", "按周", "周粒度", "按周看", "口径改成按周", "换成"),
    )


def _business_text_requests_actionability_verification(text: str) -> bool:
    return _contains_any(
        text,
        ("指导投放", "直接指导", "能不能直接", "可不可以直接", "能否直接", "有多稳", "稳健", "稳定性"),
    )


def _infer_question_families_from_requested_nodes(
    intent: dict[str, Any], requested_nodes: Iterable[str]
) -> None:
    additions = []
    if "segment_contribution" in requested_nodes:
        additions.append("segment_or_factor_attribution")
    if "user_mix_contribution" in requested_nodes:
        additions.append("segment_or_factor_attribution")
    if "high_value_user_contribution" in requested_nodes:
        additions.append("segment_or_factor_attribution")
    if "outlier_contribution" in requested_nodes:
        additions.append("anomaly_or_black_swan_review")
    if "driver_decomposition" in requested_nodes:
        additions.append("paid_amount_change_explanation")
    if "compare_periods" in requested_nodes:
        additions.append("custom_baseline_comparison")
    if not additions:
        return

    families = list(intent.get("question_families") or ())
    if not families and intent.get("question_family"):
        families.append(str(intent["question_family"]))
    secondary = list(intent.get("secondary_question_families") or ())
    primary = str(intent.get("primary_question_family") or intent.get("question_family") or "")
    for family in additions:
        if family not in families:
            families.append(family)
        if family != primary and family not in secondary:
            secondary.append(family)
    intent["question_families"] = families
    intent["secondary_question_families"] = secondary


def _intent_question_family_set(intent: Mapping[str, Any]) -> set[str]:
    values: list[Any] = [
        intent.get("question_family"),
        intent.get("primary_question_family"),
    ]
    values.extend(intent.get("question_families") or ())
    values.extend(intent.get("secondary_question_families") or ())
    return {str(value) for value in values if value}


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle.lower() in text for needle in needles)


def _align_route_output_to_requested(
    output: Mapping[str, Any],
    requested: tuple[str, ...],
) -> dict[str, Any]:
    value = dict(output)
    visible_text = json.dumps(
        {
            "route_summary": value.get("route_summary", ""),
            "expected_evidence": value.get("expected_evidence", []),
            "decision_summary": value.get("decision_summary", ""),
        },
        ensure_ascii=False,
        default=str,
    )
    stale_tokens = {
        "metric_timeseries",
        "rolling_window_compare",
        "指标时间序列",
        "滚动窗口对比",
    }
    if not any(token in visible_text for token in stale_tokens):
        return value
    labels = _capability_path_labels(requested)
    value["route_summary"] = f"本次采用{labels}完成分析，路径已按当前问题口径和可执行能力对齐。"
    value["expected_evidence"] = [
        f"{label}：产出本次业务判断需要的证据和限制说明。"
        for label in _capability_labels(requested)
    ]
    value["decision_summary"] = (
        "已移除不适合当前口径或当前未执行的候选路径，保留可执行的业务分析能力。"
    )
    return value


def _requested_node_ids(
    nodes: Any,
    *,
    excluded: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    result = []
    for item in nodes or ():
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            value = (
                item.get("capability_id")
                or item.get("capability")
                or item.get("node_id")
                or item.get("node")
                or item.get("id")
            )
        else:
            value = ""
        if value and value not in excluded:
            result.append(str(value))
    return tuple(dict.fromkeys(result))


def _target_label_from_pattern_params(pattern_params: dict[str, Any]) -> str:
    value = (
        pattern_params.get("target_phase")
        or pattern_params.get("target_group")
        or pattern_params.get("target_weekdays")
        or pattern_params.get("target_weekday")
        or pattern_params.get("target_window")
        or pattern_params.get("target_bucket")
        or "target"
    )
    return _business_label(value)


def _baseline_label_from_pattern_params(pattern_params: dict[str, Any]) -> str:
    value = (
        pattern_params.get("baseline_phase")
        or pattern_params.get("baseline_group")
        or pattern_params.get("baseline_weekdays")
        or pattern_params.get("baseline_weekday")
        or pattern_params.get("baseline_window")
        or pattern_params.get("baseline_bucket")
        or "baseline"
    )
    return _business_label(value)


def _target_label(state: WorkflowState) -> str:
    intent = state.get("intent", {})
    label = intent.get("target", {}).get("label")
    if label and not _generic_window_label(label, role="target"):
        return str(label)
    observed = _observed_window_label(state, role="target")
    if observed:
        return observed
    if label:
        return str(label)
    return _target_label_from_pattern_params(dict(intent.get("pattern_params", {})))


def _baseline_label(state: WorkflowState) -> str:
    intent = state.get("intent", {})
    label = intent.get("baseline", {}).get("label")
    if label and not _generic_window_label(label, role="baseline"):
        return str(label)
    observed = _observed_window_label(state, role="baseline")
    if observed:
        return observed
    if label:
        return str(label)
    return _baseline_label_from_pattern_params(dict(intent.get("pattern_params", {})))


def _generic_window_label(value: Any, *, role: str) -> bool:
    normalized = str(value or "").strip().lower()
    generic = {
        "target": {"target", "target_day", "目标", "目标日", "目标窗口"},
        "baseline": {
            "baseline",
            "previous_day",
            "基线",
            "基准日",
            "基准窗口",
        },
    }
    return normalized in generic[role]


def _observed_window_label(state: WorkflowState, *, role: str) -> str:
    payload_key = "target" if role == "target" else "primary_baseline"
    for evidence in state.get("evidence") or ():
        if not isinstance(evidence, Mapping):
            continue
        payload = evidence.get("typed_payload") or {}
        if not isinstance(payload, Mapping):
            continue
        window = payload.get(payload_key) or {}
        if not isinstance(window, Mapping):
            continue
        keys = [
            str(item).strip()
            for item in window.get("observation_keys") or ()
            if str(item).strip()
        ]
        if not keys:
            continue
        if len(keys) == 1:
            return keys[0]
        return f"{keys[0]} 至 {keys[-1]}"
    return ""


def _business_label(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def _execute_joint_attribution(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "execute_joint_attribution")
    evidence = list(state.get("evidence", []))
    segment = next((item for item in evidence if item.get("capability") == "segment_bridge"), None)
    evidence.append(
        _evidence_dict(
            joint_attribution(
                _capability_rows_for(state, "joint_attribution"),
                segment_evidence=segment,
                result_refs=_capability_result_refs_for(
                    state,
                    "joint_attribution",
                ),
                **_joint_attribution_params(state),
            ),
            state,
        )
    )
    state["evidence"] = evidence
    return state


def _interpret_evidence(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "interpret_evidence")
    evidence_payload = {"businessContext": _business_evidence_context(state)}
    try:
        output = _invoke_llm(
            state,
            "evidence_interpretation",
            evidence_payload,
            output_validator=lambda candidate: (
                _validate_business_factor_state_narrative(
                    candidate,
                    evidence_payload,
                    fields=(
                        "interpretation",
                        "decision_summary",
                        "evidence_boundary",
                    ),
                )
            ),
        )
        _validate_business_factor_state_narrative(
            output,
            evidence_payload,
            fields=(
                "interpretation",
                "decision_summary",
                "evidence_boundary",
            ),
        )
        state["evidence_interpretation"] = _normalize_evidence_interpretation_output(
            output,
            state,
        )
    except (WorkflowFailure, LLMOutputError) as exc:
        state["evidence_interpretation"] = {
            "status": "unavailable",
            "business_boundary": (
                "证据解读文案本轮不可用，不影响已验证的数值、方向和因素贡献。"
            ),
            "display_summary": (
                "证据解读文案本轮不可用，不影响已验证的数值、方向和因素贡献。"
            ),
            "failure_reason": str(exc),
        }
    return state


def _business_evidence_context(state: WorkflowState) -> dict[str, Any]:
    """Project authority evidence into business-only material for narrative tasks."""

    resolution = _required_claim_evidence_resolution(state)
    if "authority_verified_claims" in state:
        claims = _verified_claims(state)
    else:
        claims = _authority_claims_from_evidence(state)
    claim_slots = [
        {
            "claimSlot": f"结论{index}",
            "statement": str(claim.get("text") or ""),
            "scope": _scope_label(claim.get("scope")),
            "timeWindow": str(claim.get("time_window") or ""),
            "strength": _business_claim_strength(claim.get("claim_strength")),
        }
        for index, claim in enumerate(claims, start=1)
    ]
    unavailable = [
        {
            "conclusion": _CLAIM_INTENT_BUSINESS_SEMANTICS.get(
                claim_type,
                "该业务结论",
            ),
            "state": "当前证据不足",
        }
        for claim_type, entry in resolution["required"].items()
        if entry.get("status") != "publishable"
    ]
    boundaries = list(
        _business_limitation_reasons(
            tuple(resolution.get("material_limitations") or ())
        )
    )
    return {
        "question": {
            "metric": _business_metric_label(state),
            "scope": _scope_label(state.get("intent", {}).get("scope")),
            "timeWindow": str(state.get("intent", {}).get("time_window") or ""),
            "target": _target_label(state),
            "baseline": _baseline_label(state),
        },
        "claimSlots": claim_slots,
        "factorStates": _business_factor_states(state),
        "unavailableConclusions": unavailable,
        "boundaries": boundaries,
    }


def _business_answer_context(state: WorkflowState) -> dict[str, Any]:
    """Build the complete public input for business answer writing."""

    return {
        "questionUnderstanding": _question_understanding_sentence(state),
        "analysisPath": _analysis_path_sentence(state),
        "evidence": _business_evidence_context(state),
        "insightPortfolio": dict(state.get("diagnostic_insights") or {}),
        "causalBoundary": (
            "已对账的组成贡献可以说明主要贡献项和抵消关系；"
            "跨来源时序和渠道面板结果只支持候选关联，不能替代贡献或因果证据。"
        ),
        "answerShape": [
            "管理结论",
            "决定性证据：说明主要贡献、抵消项和增长质量",
            "关键反事实",
            "业务定位：呈现有信息量的地区、城市或其他细分结果",
            "玩法关联：区分整体时序关联与渠道内部稳健性，并明确其候选证据边界",
            "证据边界：仅保留会改变决策的限制和下一步核查",
        ],
    }


def _business_final_audit_context(state: WorkflowState) -> dict[str, Any]:
    context = _business_answer_context(state)
    evidence = context.get("evidence") or {}
    anchors: list[dict[str, str]] = []
    for claim in evidence.get("claimSlots") or ():
        if not isinstance(claim, Mapping):
            continue
        key = str(claim.get("claimSlot") or "").strip()
        summary = str(claim.get("statement") or "").strip()
        if key and summary:
            anchors.append(
                {"kind": "claim_slot", "key": key, "summary": summary}
            )
    for factor in evidence.get("factorStates") or ():
        if not isinstance(factor, Mapping):
            continue
        key = str(factor.get("factor") or "").strip()
        summary = str(factor.get("state") or "").strip()
        if key and summary:
            anchors.append(
                {"kind": "factor_state", "key": key, "summary": summary}
            )
    exact_comparisons: set[str] = set()
    for item in state.get("evidence", ()):
        if not isinstance(item, Mapping):
            continue
        capability = str(
            item.get("capability_id") or item.get("capability") or ""
        )
        payload = item.get("typed_payload") or {}
        if capability != "compare_periods" or not isinstance(payload, Mapping):
            continue
        if payload.get("target_value") is None or payload.get("baseline_value") is None:
            continue
        summary = str(
            _default_claim_from_primary_evidence(state, evidence=item).get("text")
            or ""
        ).strip()
        if not summary or summary in exact_comparisons:
            continue
        exact_comparisons.add(summary)
        anchors.append(
            {
                "kind": "verified_fact",
                "key": f"精确对比值{len(exact_comparisons)}",
                "summary": summary,
            }
        )
    causal_boundary = str(context.get("causalBoundary") or "").strip()
    if causal_boundary:
        anchors.append(
            {
                "kind": "boundary",
                "key": "原因边界",
                "summary": causal_boundary,
            }
        )
    for index, boundary in enumerate(evidence.get("boundaries") or (), start=1):
        summary = str(boundary or "").strip()
        if summary:
            anchors.append(
                {
                    "kind": "boundary",
                    "key": f"数据边界{index}",
                    "summary": summary,
                }
            )
    context["reviewAnchors"] = anchors
    return context


_BUSINESS_DISPLAY_REVIEW_MESSAGES = {
    "internal_visible_token": "业务文案出现内部技术标识，需要改成业务语言。",
    "missing_pattern_evidence": "业务答案没有清楚说明变化方向的证据。",
    "missing_driver_claim": "业务答案没有保留已验证的因素贡献结论。",
    "missing_primary_claim": "业务答案没有保留主要结论。",
    "sensitive_output_leak": "敏感输出检查未通过，当前答案不能展示。",
    "sql_security_failure": "查询安全检查未通过，当前答案不能展示。",
    "unsupported_main_claim": "主要结论超出了当前证据支持范围。",
    "verifier_evidence_contradiction": "答案与已验证证据存在矛盾。",
}


def _business_display_review(
    state: WorkflowState,
    *,
    stage: str,
) -> dict[str, Any]:
    """Expose local display findings without leaking runtime diagnostics."""

    issue_codes = [
        str(item)
        for item in state.get("final_summary_display_warnings", ())
        if str(item)
    ]
    semantic = state.get("semantic_audit") or {}
    uncoded_findings: list[str] = []
    for issue in semantic.get("issues") or ():
        if not isinstance(issue, Mapping):
            continue
        if issue.get("code"):
            issue_codes.append(str(issue["code"]))
            continue
        severity = str(issue.get("severity") or "").lower()
        uncoded_findings.append(
            "业务文案存在需要修正的事实范围或证据边界问题。"
            if severity in {"error", "critical", "blocking"}
            else "业务文案有一项可选表达建议。"
        )
    blocker_codes = _local_final_answer_hard_blockers(state)
    messages = [
        _BUSINESS_DISPLAY_REVIEW_MESSAGES.get(
            code,
            "本地展示检查发现一项需要确认的问题。",
        )
        for code in dict.fromkeys([*blocker_codes, *issue_codes])
    ]
    messages.extend(
        item for item in dict.fromkeys(uncoded_findings) if item not in messages
    )
    return {
        "reviewStage": (
            "业务文案一致性检查"
            if stage == "semantic"
            else "最终展示检查"
        ),
        "localStatus": "需要处理" if blocker_codes or messages else "已通过",
        "findings": messages,
        "reviewScope": [
            "结论与业务证据是否一致",
            "是否保留时间、范围和数据边界",
            "是否出现过强原因表述或内部技术语言",
        ],
    }


def _business_claim_strength(value: Any) -> str:
    return {
        "strong": "强证据",
        "high": "高强度证据",
        "medium": "中等强度证据",
        "observed": "已观测",
        "insufficient": "证据不足",
    }.get(str(value or ""), "有边界的证据")


def _business_factor_states(state: WorkflowState) -> list[dict[str, Any]]:
    factors: list[dict[str, Any]] = []
    for evidence in state.get("evidence") or ():
        if not isinstance(evidence, Mapping):
            continue
        payload = evidence.get("typed_payload") or {}
        decompositions = payload.get("decompositions") if isinstance(payload, Mapping) else ()
        for decomposition in decompositions or ():
            if not isinstance(decomposition, Mapping):
                continue
            contributions = {
                str(item.get("component_id") or ""): item
                for item in decomposition.get("core_factor_contributions") or ()
                if isinstance(item, Mapping)
            }
            for change in decomposition.get("component_changes") or ():
                if not isinstance(change, Mapping):
                    continue
                factor = str(change.get("business_name") or "").strip()
                if not factor:
                    continue
                component = str(change.get("component_id") or "")
                contribution = contributions.get(component)
                if change.get("observed") is False:
                    state_label = "缺少独立观测，本轮按不变处理"
                elif contribution is not None:
                    state_label = "已量化贡献"
                else:
                    state_label = "已观察变化，贡献尚未量化"
                item = {"factor": factor, "state": state_label}
                if change.get("observed") is not False:
                    item.update(
                        {
                            "baseline": change.get("baseline_value"),
                            "target": change.get("target_value"),
                            "change": change.get("delta"),
                            "changeRate": change.get("delta_ratio"),
                        }
                    )
                if contribution is not None and change.get("observed") is not False:
                    item.update(
                        {
                            "contribution": contribution.get("contribution"),
                            "contributionShare": contribution.get(
                                "contribution_share"
                            ),
                        }
                    )
                factors.append(item)
    return factors


def _validate_business_factor_state_narrative(
    output: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    fields: Sequence[str],
) -> None:
    context = payload.get("businessContext") or {}
    evidence = context.get("evidence") if isinstance(context, Mapping) else {}
    factor_states = (
        evidence.get("factorStates")
        if isinstance(evidence, Mapping)
        else None
    ) or (
        context.get("factorStates")
        if isinstance(context, Mapping)
        else None
    ) or ()
    if not factor_states:
        return
    narrative_values: list[str] = []
    for field in fields:
        value = output.get(field)
        if isinstance(value, str):
            narrative_values.append(value)
        elif isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            narrative_values.extend(
                item for item in value if isinstance(item, str)
            )
    narrative = "\n".join(narrative_values)
    quantified_states = [
        item
        for item in factor_states
        if isinstance(item, Mapping)
        and str(item.get("state") or "") == "已量化贡献"
    ]
    quantified_shares = [
        _as_float(item.get("contributionShare"))
        for item in quantified_states
    ]
    core_contribution_reconciled = bool(quantified_states) and all(
        share is not None for share in quantified_shares
    ) and abs(sum(share or 0.0 for share in quantified_shares) - 1.0) <= 1e-6
    if core_contribution_reconciled and re.search(
        r"(?:整体|全部|三因素|核心).{0,12}(?:归因|贡献|拆解|分解)"
        r".{0,12}(?:存在|仍有|还有).{0,6}(?:未覆盖|缺口|遗漏)"
        r"|(?:整体|全部|三因素|核心).{0,12}(?:归因|贡献|拆解|分解)"
        r".{0,8}(?:不完整|未完全覆盖|未覆盖完整)",
        narrative,
    ):
        raise LLMOutputError(
            "accounting_reconciliation_narrative_conflict"
        )
    sentences = [
        sentence.strip()
        for sentence in re.split(
            r"[。；;!?？\n]+|(?<!\d)\.(?!\d)",
            narrative,
        )
        if sentence.strip()
    ]
    missing_observation = re.compile(
        r"(?:缺(?:少|乏)?|没有|无|未).{0,6}(?:独立)?观测"
    )
    neutral_treatment = re.compile(
        r"按不变|视为不变|不变假设|假设不变|实际变化未知"
    )
    observed_marker = re.compile(r"已观察|观察到|观察项|已观测|观测到")
    owned_observed_marker = re.compile(r"(?:的|其)(?:观察|观测)变化")
    negated_observed_marker = re.compile(
        r"(?:不(?:视为|代表|等于|是)|并非).{0,8}"
        r"(?:已观察|已观测|观察到|观测到)"
    )
    unquantified_contribution = re.compile(
        r"(?:变化|贡献).{0,10}"
        r"(?:尚?未(?:单独)?量化|未纳入.{0,6}(?:量化|贡献)|未计入)"
    )
    quantified_contribution = re.compile(
        r"已量化贡献|贡献.{0,8}(?:已(?:经)?量化|为?[-+]?\d)"
    )
    quantified_decomposition_reference = re.compile(
        r"(?:不(?:会)?|未)(?:改变|影响|撤销|削弱|否定).{0,8}"
        r"已量化(?:的)?(?:三因素|核心因素|核心|三项).{0,6}"
        r"(?:贡献|拆解|分解)"
    )
    demonstrative_group = re.compile(
        r"(?:这些|上述)(?:因素|指标|组成项|驱动项)|前者|后者|两者|三者"
    )
    exhaustive_group = re.compile(
        r"(?:其他|其余)(?:因素|指标|组成项|驱动项)"
    )
    factor_states_by_name = {
        str(item.get("factor") or "").strip(): str(item.get("state") or "").strip()
        for item in factor_states
        if isinstance(item, Mapping) and str(item.get("factor") or "").strip()
    }

    def mentioned_factors(value: str) -> list[str]:
        occupied_spans: list[tuple[int, int]] = []
        matches: list[tuple[int, str]] = []
        for factor in sorted(factor_states_by_name, key=len, reverse=True):
            for match in re.finditer(re.escape(factor), value):
                span = match.span()
                if any(
                    span[0] < occupied[1] and occupied[0] < span[1]
                    for occupied in occupied_spans
                ):
                    continue
                occupied_spans.append(span)
                matches.append((span[0], factor))
        return [factor for _, factor in sorted(matches)]

    def predicate_names(value: str) -> set[str]:
        predicates: set[str] = set()
        if (
            observed_marker.search(value) or owned_observed_marker.search(value)
        ) and not negated_observed_marker.search(value):
            predicates.add("observed")
        if quantified_contribution.search(
            value
        ) and not quantified_decomposition_reference.search(value):
            predicates.add("quantified")
        if unquantified_contribution.search(value):
            predicates.add("unquantified")
        if missing_observation.search(value):
            predicates.add("unobserved")
        if neutral_treatment.search(value):
            predicates.add("neutral")
        return predicates

    def validate_predicates(
        factors: Sequence[str],
        predicates: set[str],
    ) -> None:
        if not factors or not predicates:
            return
        forbidden_by_state = {
            "已量化贡献": {"unquantified", "unobserved", "neutral"},
            "已观察变化，贡献尚未量化": {
                "quantified",
                "unobserved",
                "neutral",
            },
            "缺少独立观测，本轮按不变处理": {
                "observed",
                "quantified",
                "unquantified",
            },
        }
        for factor in factors:
            state_label = factor_states_by_name.get(factor, "")
            if predicates & forbidden_by_state.get(state_label, set()):
                raise LLMOutputError(
                    f"factor_state_narrative_conflict:{factor}"
                )

    all_factors = list(factor_states_by_name)
    for sentence in sentences:
        antecedent: list[str] = []
        pending_factor_list = False
        clauses: list[str] = []
        for raw_clause in re.split(r"，|、|(?<!\d),(?!\d)", sentence):
            raw_clause = raw_clause.strip()
            if not raw_clause:
                continue
            coordinated = [
                item.strip()
                for item in re.split(r"(?:以及|和|及)", raw_clause)
                if item.strip()
            ]
            if len(coordinated) > 1 and all(
                mentioned_factors(item) and predicate_names(item)
                for item in coordinated
            ):
                clauses.extend(coordinated)
            else:
                clauses.append(raw_clause)
        for clause in clauses:
            if not clause:
                continue
            explicit = mentioned_factors(clause)
            predicates = predicate_names(clause)
            relationship_subjects: list[str] = []
            if "作为" in clause and re.search(
                r"作为.{0,20}(?:下钻|细分|拆分|观察)",
                clause,
            ):
                relationship_subjects = mentioned_factors(
                    clause.split("作为", 1)[0]
                )
            explicit_subjects = relationship_subjects or explicit
            targets = list(explicit_subjects)
            if explicit:
                if relationship_subjects:
                    antecedent = list(relationship_subjects)
                    pending_factor_list = False
                elif not predicates:
                    antecedent = list(
                        dict.fromkeys([*antecedent, *explicit_subjects])
                    )
                    pending_factor_list = True
                elif pending_factor_list:
                    antecedent = list(
                        dict.fromkeys([*antecedent, *explicit_subjects])
                    )
                    pending_factor_list = False
                else:
                    antecedent = list(explicit_subjects)
                    pending_factor_list = False
            elif demonstrative_group.search(clause):
                if "前者" in clause and antecedent:
                    targets = [antecedent[0]]
                elif "后者" in clause and antecedent:
                    targets = [antecedent[-1]]
                elif "两者" in clause and antecedent:
                    targets = antecedent[-2:]
                elif "三者" in clause and antecedent:
                    targets = antecedent[-3:]
                else:
                    targets = list(antecedent or all_factors)
            elif exhaustive_group.search(clause):
                targets = [
                    factor for factor in all_factors if factor not in antecedent
                ] or list(all_factors)
            elif predicates and antecedent:
                targets = [antecedent[-1]]
            validate_predicates(targets, predicates)


def _audit_causal_implications(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "audit_causal_implications")
    dossier = _build_causal_evidence_dossier(state)
    state["causal_evidence_dossier"] = dossier
    payload = _business_causal_audit_payload(state)
    try:
        output = _invoke_llm(
            state,
            "causal_audit",
            payload,
            output_validator=lambda candidate: (
                _validate_causal_audit_provider_output(candidate, payload)
            ),
        )
        _validate_causal_audit_provider_output(output, payload)
        state["causal_audit"] = _normalize_causal_audit_provider_output(output)
    except (WorkflowFailure, LLMOutputError) as exc:
        state["causal_audit"] = {
            "status": "unavailable",
            "business_boundary": (
                "深层原因审阅本轮不可用，不影响已验证的会计贡献和变化方向。"
            ),
            "failure_reason": str(exc),
        }
    return state


def _validate_causal_audit_provider_output(
    output: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> None:
    expected_fields = {
        "causal_assessment",
        "publishable_wording",
        "supporting_reasons",
        "evidence_limit",
        "display_summary",
    }
    if set(output) != expected_fields:
        raise LLMOutputError("causal_audit_top_level_contract_invalid")
    if str(output.get("causal_assessment") or "") not in {
        "causal_supported",
        "plausible_mechanism",
        "directional_association",
        "candidate_hypothesis",
        "mixed_or_confounded",
        "not_supported",
        "needs_more_evidence",
    }:
        raise LLMOutputError("causal_audit_assessment_invalid")
    supporting_reasons = _causal_supporting_reason_values(
        output.get("supporting_reasons")
    )
    if supporting_reasons is None:
        raise LLMOutputError("causal_audit_supporting_reasons_invalid")
    evidence_limit = output.get("evidence_limit")
    if not isinstance(evidence_limit, str) or not evidence_limit.strip():
        raise LLMOutputError("causal_audit_evidence_limit_invalid")
    _validate_business_factor_state_narrative(
        output,
        payload,
        fields=(
            "publishable_wording",
            "supporting_reasons",
            "evidence_limit",
            "display_summary",
        ),
    )

    causal_review = payload.get("causalReview") or {}
    mechanism_evidence = str(
        causal_review.get("mechanismEvidence") or ""
    )
    evidence_absent = any(
        marker in mechanism_evidence
        for marker in (
            "当前没有独立",
            "没有独立",
            "缺少独立",
            "no independent",
        )
    )
    if not evidence_absent:
        return
    if output.get("causal_assessment") != "not_supported":
        raise LLMOutputError("causal_audit_assessment_exceeds_evidence")
    narrative = "\n".join(
        [
            str(output.get("publishable_wording") or ""),
            *supporting_reasons,
            str(output.get("evidence_limit") or ""),
            str(output.get("display_summary") or ""),
        ]
    )
    if re.search(
        r"可能|或许|也许|例如|比如|譬如|候选|替代解释|若实际|假设为",
        narrative,
    ):
        raise LLMOutputError("causal_audit_ungrounded_mechanism")


def _causal_supporting_reason_values(value: Any) -> list[str] | None:
    if isinstance(value, str):
        normalized = value.strip()
        return [normalized] if normalized else None
    if isinstance(value, list) and all(
        isinstance(item, str) and item.strip()
        for item in value
    ):
        return [item.strip() for item in value]
    return None


def _normalize_causal_audit_provider_output(
    output: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = dict(output)
    supporting_reasons = _causal_supporting_reason_values(
        output.get("supporting_reasons")
    )
    if supporting_reasons is None:
        raise LLMOutputError("causal_audit_supporting_reasons_invalid")
    normalized["supporting_reasons"] = supporting_reasons
    return normalized


def _business_causal_audit_payload(state: WorkflowState) -> dict[str, Any]:
    causal_evidence_available = any(
        isinstance(item, Mapping)
        and item.get("evidence_type") == "causal_evidence"
        and _evidence_established(item)
        for item in state.get("evidence") or ()
    )
    return {
        "businessContext": _business_answer_context(state),
        "causalReview": {
            "reviewGoal": "区分已量化的会计贡献与更深层业务机制。",
            "accountingBoundary": (
                "对账通过的组成贡献可以说明主要贡献项和抵消关系。"
            ),
            "mechanismEvidence": (
                "当前已有独立机制证据，可按其强度审阅原因表述。"
                if causal_evidence_available
                else "当前没有独立的对照、时间先后或机制证据。"
            ),
            "publicationBoundary": (
                "机制证据不足只限制深层原因表述，不撤销已验证的会计贡献。"
            ),
        },
    }


def _build_causal_evidence_dossier(state: WorkflowState) -> dict[str, Any]:
    intent = state.get("intent", {})
    pattern = _pattern_evidence(state)
    payload = pattern.get("typed_payload", {})
    limitations = list(state.get("evidence_brief", {}).get("limitations", ()))
    median_uplift = payload.get("median_uplift")
    return {
        "target_claim": intent.get("target_claim", ""),
        "question_family": intent.get("question_family", ""),
        "scope": intent.get("scope", ""),
        "time_window": intent.get("time_window", ""),
        "observed_pattern": {
            "metric": _business_metric_label(state),
            "pattern_family": payload.get("pattern_family") or intent.get("pattern_family"),
            "effect_size": median_uplift,
            "direction": _direction_from_median_uplift(median_uplift),
            "direction_ratio": payload.get("direction_ratio"),
            "comparable_periods": payload.get("comparable_periods"),
            "strength": pattern.get("strength"),
            "wording_limit": pattern.get("wording_limit"),
            "limitations": limitations,
        },
        "temporal_order": {
            "known": False,
            "summary": "当前证据未提供可验证的先后顺序。",
        },
        "comparison_context": {
            "baseline": _baseline_label(state),
            "target": _target_label(state),
            "control_or_counterfactual": "none",
            "summary": "当前证据来自已执行的业务对比路径。",
        },
        "segment_consistency": [],
        "event_overlap": [],
        "alternative_explanations": [],
        "negative_evidence": limitations,
        "missing_evidence": [
            "缺少独立因果验证、对照或机制证据。",
        ],
        "data_limits": limitations,
    }


def _direction_from_median_uplift(value: Any) -> str:
    if value is None:
        return "unknown"
    if value > 0:
        return "target_higher"
    if value < 0:
        return "target_lower"
    return "none"


def _normalize_evidence_interpretation_output(
    output: dict[str, Any], state: WorkflowState
) -> dict[str, Any]:
    normalized = {
        key: _normalize_evidence_interpretation_text(
            str(output.get(key) or ""),
            state,
        )
        for key in (
            "interpretation",
            "decision_summary",
            "evidence_boundary",
        )
    }
    normalized["display_summary"] = _authoritative_evidence_display_summary(
        state,
    )
    return normalized


def _authoritative_evidence_display_summary(
    state: WorkflowState,
) -> str:
    if "authority_verified_claims" in state:
        claims = _verified_claims(state)
    else:
        claims = _authority_claims_from_evidence(state)
    statements = [
        _normalize_evidence_interpretation_text(
            str(item.get("text") or "").strip(),
            state,
        )
        for item in claims
        if isinstance(item, Mapping) and str(item.get("text") or "").strip()
    ]
    return " ".join(statements) or "当前证据尚未形成可发布的业务结论。"


def _normalize_evidence_interpretation_text(value: str, state: WorkflowState) -> str:
    normalized = _normalize_custom_baseline_direction(value, state)
    if _pattern_has_negative_answer_evidence(state):
        normalized = _businessize_negative_pattern_text(normalized)
    return normalized


def _businessize_negative_pattern_text(value: str) -> str:
    value = value.replace("目标索赔", "目标声明")
    value = value.replace("方向比", "方向一致性")
    value = value.replace("90个数据点", "90个阶段聚合点")
    value = re.sub(
        r"中位提升为-(\d+(?:\.\d+)?)%",
        r"中位变化为下降 \1%",
        value,
    )
    value = re.sub(
        r"全部月份均未超过重要性阈值（?0?\.03）?",
        "正向月份也未达到当前重要性阈值",
        value,
    )
    value = value.replace("低于重要性阈值的限制", "变化幅度不足以支持目标声明")
    return value


def _normalize_custom_baseline_direction(value: str, state: WorkflowState) -> str:
    intent = state.get("intent", {})
    if intent.get("pattern_family") != "custom_baseline":
        return value
    baseline = str(intent.get("baseline", {}).get("label") or "")
    target = str(intent.get("target", {}).get("label") or "")
    if not baseline or not target:
        return value
    return (
        value.replace(f"{baseline} 相比 {target}", f"{target} 相比 {baseline}")
        .replace(f"{baseline}相比{target}", f"{target} 相比 {baseline}")
        .replace("中位数提升", "对比提升")
        .replace("中位数下降", "对比下降")
    )


def _synthesize_answer(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "synthesize_answer")
    state["draft_claims"] = _authority_claims_from_evidence(state)
    answer_payload = {"businessContext": _business_answer_context(state)}
    provider_output = _invoke_llm(
        state,
        "answer_synthesis",
        answer_payload,
        output_validator=_validate_answer_draft_provider_output,
        defer_narrative_validation=True,
    )
    output = _normalized_answer_draft_provider_output(provider_output)
    state["answer_text"] = _weaken_unsupported_causal_wording(output.get("answer_text", ""))
    return state


_PROVIDER_OWNED_CLAIM_FIELDS = frozenset(
    {
        "claims",
        "draft_claims",
        "verified_claims",
        "authority_verified_claims",
        "claim_slots",
        "claimSlots",
    }
)


def _validate_answer_draft_provider_output(output: Mapping[str, Any]) -> None:
    _normalized_answer_draft_provider_output(output)


def _normalized_answer_draft_provider_output(
    output: Mapping[str, Any],
) -> dict[str, Any]:
    if _PROVIDER_OWNED_CLAIM_FIELDS.intersection(output):
        raise LLMOutputError("answer_synthesis_returned_canonical_claims")
    provider_narrative = {
        key: output[key]
        for key in ("answer_text", "display_summary")
        if key in output
    }
    normalized = _localize_narrative_fields(provider_narrative)
    if not isinstance(normalized, dict):
        raise LLMOutputError("llm_output_not_object")
    answer_text = normalized.get("answer_text")
    if not isinstance(answer_text, str) or not answer_text.strip():
        raise LLMOutputError("llm_narrative_invalid:answer_text")
    return normalized


def _semantic_audit(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "semantic_audit")
    audit_payload = {
        "answerText": state.get("answer_text", ""),
        "businessContext": _business_answer_context(state),
        "displayReview": _business_display_review(state, stage="semantic"),
    }
    state["semantic_audit"] = _normalize_semantic_audit_decision(
        _invoke_llm(state, "semantic_audit", audit_payload)
    )
    audit = state["semantic_audit"]
    if _semantic_audit_requires_revision(audit):
        state["retry_context"] = _retry_context(
            "semantic_audit",
            "semantic_audit",
            audit.get("issues", []) or audit.get("audit_status", ""),
        )
    return state


def _hard_verify_answer(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "hard_verify_answer")
    package = _build_answer_package_from_state(state)
    verifier = package["admin_audit"]["verifier"]
    state["verifier"] = verifier
    state["authority_verified_claims"] = [
        dict(item)
        for item in package.get("admin_audit", {}).get(
            "verified_claims", ()
        )
        if isinstance(item, Mapping)
    ]
    state["authority_verified_claims_digest"] = canonical_digest(
        state["authority_verified_claims"]
    )
    if verifier.get("errors"):
        state["retry_context"] = _retry_context(
            "hard_verify_answer",
            "verifier",
            verifier.get("errors", []),
        )
    return state


def _repair_answer(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "repair_answer")
    state["answer_repair_attempts"] = state.get("answer_repair_attempts", 0) + 1
    failure_type = str(
        state.get("retry_context", {}).get("failure_type") or ""
    )
    if failure_type == "semantic_audit":
        state["semantic_repair_attempts"] = (
            state.get("semantic_repair_attempts", 0) + 1
        )
    elif failure_type == "verifier":
        state["verifier_repair_attempts"] = (
            state.get("verifier_repair_attempts", 0) + 1
        )
    provider_output = _invoke_llm(
        state,
        "answer_repair",
        {
            "answerText": state.get("answer_text", ""),
            "businessContext": _business_answer_context(state),
            "displayReview": _business_display_review(state, stage="semantic"),
        },
        output_validator=_validate_answer_draft_provider_output,
        defer_narrative_validation=True,
    )
    output = _normalized_answer_draft_provider_output(provider_output)
    state["answer_text"] = _weaken_unsupported_causal_wording(
        output.get("answer_text", state.get("answer_text", ""))
    )
    return state


def _generate_degraded_explanation(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "generate_degraded_explanation")
    contract_gap_diagnostics = _refresh_contract_gap_diagnostics(state)
    explanation_payload = {
        "intent": state.get("intent", {}),
        "analysis_contract": state.get("request", {}).get("analysis_contract") or {},
        "evidence_brief": state.get("evidence_brief", {}),
        "verifier": state.get("verifier", {}),
        "contract_gap_diagnostics": contract_gap_diagnostics,
    }
    state["final_explanation"] = _invoke_terminal_explanation(
        state,
        task="degraded_explanation",
        payload=explanation_payload,
        status="degraded",
    )
    rejected_answer = bool(state.get("verifier", {}).get("errors")) or str(
        state.get("retry_context", {}).get("failure_type") or ""
    ) in {"semantic_audit", "verifier"}
    if rejected_answer:
        preserved_claims = _preserved_authority_claims(state)
        state["draft_claims"] = preserved_claims
        if not preserved_claims:
            state["answer_text"] = str(
                state["final_explanation"].get("explanation") or ""
            )
    state["verifier"] = {"status": "terminal_explanation", "errors": [], "warnings": []}
    if "evidence" not in state:
        state["evidence"] = []
    if "draft_claims" not in state:
        state["draft_claims"] = []
    _ensure_degraded_audit(state)
    return state


def _ensure_degraded_audit(state: WorkflowState) -> None:
    evidence_items = list(state.get("evidence") or [])
    if not evidence_items:
        evidence_items.append(_degraded_boundary_evidence(state))
    state["evidence"] = evidence_items

    runtime_result = state.get("analysis_runtime_result")
    if runtime_result is not None and runtime_result.status == "blocked":
        state["draft_claims"] = []
        return

    draft_claims = list(state.get("draft_claims") or [])
    if draft_claims:
        state["draft_claims"] = draft_claims
        return
    state["draft_claims"] = []


def _degraded_boundary_evidence(state: WorkflowState) -> dict[str, Any]:
    limitations = _degraded_limitations(state)
    return {
        "evidence_ref": f"degraded_boundary:{state['run_id']}",
        "capability_id": "answer_verify",
        "evidence_type": "insufficient",
        "strength": "insufficient",
        "wording_limit": "insufficient",
        "limitations": limitations,
        "result_refs": [state.get("sql_hash", "")],
        "sql_hashes": [state.get("sql_hash", "")],
        "typed_payload": {
            "status": "degraded",
            "scope": state["intent"]["scope"],
            "time_window": state["intent"]["time_window"],
            "limitations": limitations,
            "repair_path": _terminal_repair_path(state, "degraded"),
        },
    }


def _degraded_limitations(state: WorkflowState) -> list[str]:
    limitations = list(state.get("evidence_brief", {}).get("limitations") or [])
    if limitations:
        return limitations
    result = []
    for item in state.get("evidence", []):
        for limitation in item.get("limitations", ()) or ():
            if limitation not in result:
                result.append(limitation)
    return result or ["insufficient_evidence"]


def _generate_blocked_explanation(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "generate_blocked_explanation")
    if "final_explanation" not in state:
        contract_gap_diagnostics = _refresh_contract_gap_diagnostics(state)
        data_availability_evidence = _blocked_data_availability_evidence(state)
        explanation_payload = {
            "intent": state.get("intent", {}),
            "boundary_decision": state.get("boundary_decision", {}),
            "validator_results": state.get("validator_results", []),
            "contract_gap_diagnostics": contract_gap_diagnostics,
            "data_availability_boundary": data_availability_evidence.get(
                "typed_payload", {}
            ),
        }
        state["final_explanation"] = _invoke_terminal_explanation(
            state,
            task="blocked_explanation",
            payload=explanation_payload,
            status="blocked",
        )
        state["verifier"] = {"status": "terminal_explanation", "errors": [], "warnings": []}
    if "evidence" not in state:
        state["evidence"] = []
    if "draft_claims" not in state:
        state["draft_claims"] = []
    _ensure_blocked_boundary_audit(state)
    return state


def _invoke_terminal_explanation(
    state: WorkflowState,
    *,
    task: str,
    payload: Mapping[str, Any],
    status: str,
) -> dict[str, Any]:
    if task == "blocked_explanation":
        output = _invoke_llm(
            state,
            task,
            dict(payload),
            output_validator=_validate_blocked_explanation_provider_output,
            defer_narrative_validation=True,
        )
        output = _normalized_blocked_explanation_provider_output(output)
    elif task == "degraded_explanation":
        output = _invoke_llm(
            state,
            task,
            dict(payload),
            output_validator=_validate_degraded_explanation_provider_output,
            defer_narrative_validation=True,
        )
        output = _normalized_degraded_explanation_provider_output(output)
    else:
        output = _invoke_llm(state, task, dict(payload))
    return _sanitize_terminal_explanation(output, state, status)


def _validate_blocked_explanation_provider_output(
    output: Mapping[str, Any],
) -> None:
    _normalized_blocked_explanation_provider_output(output)


def _normalized_blocked_explanation_provider_output(
    output: Mapping[str, Any],
) -> dict[str, Any]:
    provider_narrative = {
        key: value
        for key, value in output.items()
        if key != "owner"
    }
    normalized = _localize_narrative_fields(provider_narrative)
    if not isinstance(normalized, dict):
        raise LLMOutputError("llm_output_not_object")
    return normalized


def _validate_degraded_explanation_provider_output(
    output: Mapping[str, Any],
) -> None:
    _normalized_degraded_explanation_provider_output(output)


def _normalized_degraded_explanation_provider_output(
    output: Mapping[str, Any],
) -> dict[str, Any]:
    provider_narrative = {
        key: output[key]
        for key in ("explanation", "repair_path", "display_summary")
        if key in output
    }
    normalized = _localize_narrative_fields(provider_narrative)
    if not isinstance(normalized, dict):
        raise LLMOutputError("llm_output_not_object")
    return normalized


def _ensure_blocked_boundary_audit(state: WorkflowState) -> None:
    for evidence, claim_builder in (
        (
            _blocked_data_availability_evidence(state),
            _blocked_validator_boundary_claim,
        ),
        (_blocked_validator_boundary_evidence(state), _blocked_validator_boundary_claim),
        (_blocked_contract_gap_evidence(state), _blocked_contract_gap_claim),
        (_blocked_coverage_evidence(state), _blocked_coverage_claim),
    ):
        if not evidence:
            continue
        _append_blocked_boundary_audit(state, evidence, claim_builder)
        return


def _append_blocked_boundary_audit(
    state: WorkflowState,
    evidence: Mapping[str, Any],
    claim_builder: Any,
) -> None:
    evidence = dict(evidence)

    evidence_items = list(state.get("evidence") or [])
    evidence_ref = str(evidence["evidence_ref"])
    if not any(item.get("evidence_ref") == evidence_ref for item in evidence_items):
        evidence_items.append(evidence)
    state["evidence"] = evidence_items

    # A hard-boundary explanation is visible process state, not a business
    # claim. Keep typed evidence for audit and publish zero claims.
    state["draft_claims"] = []


def _blocked_data_availability_evidence(
    state: WorkflowState,
) -> dict[str, Any]:
    decisions = tuple(
        dict(item)
        for item in state.get("query_repair_decisions") or ()
        if isinstance(item, Mapping)
    )
    terminal_decisions = tuple(
        item
        for item in decisions
        if str(item.get("action") or "") == "block"
        and str(item.get("reason") or "") == "window_coverage_failure"
    )
    if not terminal_decisions:
        return {}

    request = state.get("request") or {}
    analysis_contract = _runtime_record_projection(
        request.get("analysis_contract")
    )
    windows = tuple(
        dict(item)
        for item in analysis_contract.get("resolved_windows") or ()
        if isinstance(item, Mapping)
    )
    target_date = next(
        (
            str(window.get("label") or window.get("start_inclusive") or "")
            for window in windows
            if str(window.get("role") or "") == "target"
        ),
        str(state.get("intent", {}).get("time_window") or ""),
    )
    baseline_dates = list(
        dict.fromkeys(
            str(window.get("label") or window.get("start_inclusive") or "")
            for window in windows
            if str(window.get("role") or "") == "baseline"
            and str(window.get("label") or window.get("start_inclusive") or "")
        )
    )

    runtime_result = state.get("analysis_runtime_result")
    typed_gaps = tuple(getattr(runtime_result, "typed_gaps", ()) or ())
    latest_complete_dates = list(
        dict.fromkeys(
            str(
                (gap.get("diagnostic_context") or {}).get(
                    "latest_complete_business_date"
                )
                or ""
            )
            for gap in typed_gaps
            if isinstance(gap, Mapping)
            and str(gap.get("gap_type") or "")
            == "window_data_unavailable"
            and isinstance(gap.get("diagnostic_context"), Mapping)
            and str(
                gap["diagnostic_context"].get(
                    "latest_complete_business_date"
                )
                or ""
            )
        )
    )
    query_contracts = tuple(
        projected
        for item in request.get("query_contracts") or ()
        if (projected := _runtime_record_projection(item))
    )
    query_results = tuple(
        projected
        for item in request.get("query_results") or ()
        if (projected := _runtime_record_projection(item))
    )
    completeness_reports = tuple(
        projected
        for item in request.get("completeness_reports") or ()
        if (projected := _runtime_record_projection(item))
    )
    query_contract_refs = list(
        dict.fromkeys(
            str(
                item.get("query_contract_id")
                or item.get("query_contract_ref")
                or ""
            )
            for item in query_contracts
            if str(
                item.get("query_contract_id")
                or item.get("query_contract_ref")
                or ""
            )
        )
    )
    result_refs = list(
        dict.fromkeys(
            str(item.get("result_ref") or "")
            for item in query_results
            if str(item.get("result_ref") or "")
        )
    )
    completeness_refs = list(
        dict.fromkeys(
            str(item.get("report_ref") or "")
            for item in completeness_reports
            if str(item.get("report_ref") or "")
        )
    )
    limitations = list(
        dict.fromkeys(
            str(reason)
            for decision in terminal_decisions
            for reason in decision.get("failure_reasons") or ()
            if str(reason)
        )
    )
    return {
        "evidence_ref": (
            f"blocked_boundary:{state['run_id']}:data_availability"
        ),
        "capability_id": "answer_verify",
        "evidence_type": "insufficient",
        "strength": "insufficient",
        "wording_limit": "insufficient",
        "limitations": limitations or ["window_data_unavailable"],
        "result_refs": result_refs,
        "sql_hashes": [],
        "typed_payload": {
            "status": "data_unavailable_for_bound_windows",
            "scope": state.get("intent", {}).get("scope", ""),
            "target_date": target_date,
            "baseline_dates": baseline_dates,
            "latest_complete_business_dates": latest_complete_dates,
            "resolved_windows": list(windows),
            "query_contract_refs": query_contract_refs,
            "result_refs": result_refs,
            "completeness_refs": completeness_refs,
            "completeness_reports": list(completeness_reports),
            "repair_path": (
                "等待数据更新后发起新分析；如需更换日期，请明确提出新的业务窗口"
            ),
        },
    }


def _runtime_record_projection(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        projected = to_dict()
        return dict(projected) if isinstance(projected, Mapping) else {}
    if is_dataclass(value):
        return asdict(value)
    return {}


def _blocked_coverage_evidence(state: WorkflowState) -> dict[str, Any]:
    coverage = state.get("coverage_interpretation") or {}
    if coverage.get("coverage_status") != "blocked":
        return {}
    local_reason = str(coverage.get("local_block_reason") or "coverage_blocked")
    reason_text = _coverage_block_reason_text(coverage)
    return {
        "evidence_ref": f"coverage_block:{state['run_id']}",
        "capability_id": "data_quality_profile",
        "evidence_type": "insufficient",
        "strength": "insufficient",
        "wording_limit": "insufficient",
        "limitations": [local_reason],
        "result_refs": [state.get("sql_hash", "")],
        "sql_hashes": [state.get("sql_hash", "")],
        "typed_payload": {
            "scope": state["intent"]["scope"],
            "time_window": state["intent"]["time_window"],
            "coverage_status": coverage.get("coverage_status"),
            "local_block_reason": local_reason,
            "business_impact": reason_text,
            "decision_summary": coverage.get("decision_summary") or "",
            "repair_path": _terminal_repair_path(state, "blocked"),
        },
    }


def _blocked_contract_gap_evidence(state: WorkflowState) -> dict[str, Any]:
    gap = _blocking_contract_gap(state)
    if not gap:
        return {}
    limitations = [str(gap.get("gap_id") or "contract_gap_missing")]
    if gap.get("status"):
        limitations.append(str(gap["status"]))
    return {
        "evidence_ref": f"blocked_boundary:{state['run_id']}:contract_gap",
        "capability_id": "answer_verify",
        "evidence_type": "insufficient",
        "strength": "insufficient",
        "wording_limit": "insufficient",
        "limitations": limitations,
        "result_refs": [state.get("sql_hash", "")],
        "sql_hashes": [state.get("sql_hash", "")],
        "typed_payload": {
            "status": "blocked",
            "scope": state["intent"]["scope"],
            "time_window": state["intent"]["time_window"],
            "boundary_type": "contract_gap",
            "gap_id": str(gap.get("gap_id") or ""),
            "gap_status": str(gap.get("status") or ""),
            "claim_effect": str(gap.get("claim_effect") or ""),
            "owner": str(gap.get("owner") or ""),
            "repair_path": str(gap.get("repair_path") or _terminal_repair_path(state, "blocked")),
        },
    }


def _blocking_contract_gap(state: WorkflowState) -> dict[str, Any]:
    diagnostics = state.get("contract_gap_diagnostics")
    if diagnostics is None:
        diagnostics = _refresh_contract_gap_diagnostics(state)
    for item in diagnostics or ():
        if not isinstance(item, Mapping):
            continue
        claim_effect = str(item.get("claim_effect") or "")
        status = str(item.get("status") or "")
        if claim_effect.startswith("block_") or status in {
            "data_absent",
            "unsupported_grain",
        }:
            return dict(item)
    return {}


def _blocked_contract_gap_claim(state: WorkflowState, evidence_ref: str) -> dict[str, Any]:
    reason = _blocked_contract_gap_reason(_blocking_contract_gap(state))
    return _with_claim_audit(
        state,
        {
            "text": f"当前主业务结论被明确的数据或合同缺口阻断；{reason}。",
            "evidence_refs": [evidence_ref],
            "numbers": {},
            "scope": state["intent"]["scope"],
            "time_window": state["intent"]["time_window"],
            "claim_strength": "insufficient",
        },
    )


def _blocked_contract_gap_reason(gap: Mapping[str, Any]) -> str:
    status = str(gap.get("status") or "")
    if status == "data_absent":
        return "依赖的关键数据当前缺失，本轮只能确认边界存在，不能继续判断具体业务影响"
    if status == "unsupported_grain":
        return "当前合同只支持更粗粒度，本轮不能在该口径下发布结论"
    return "依赖的合同边界尚未满足，本轮不能发布主业务结论"


def _blocked_validator_boundary_evidence(state: WorkflowState) -> dict[str, Any]:
    failed = [
        item
        for item in state.get("validator_results", ())
        if isinstance(item, Mapping) and not item.get("ok", True)
    ]
    if not failed:
        return {}
    validators = [str(item.get("validator") or "validator") for item in failed]
    return {
        "evidence_ref": f"blocked_boundary:{state['run_id']}:validator",
        "capability_id": "answer_verify",
        "evidence_type": "insufficient",
        "strength": "insufficient",
        "wording_limit": "insufficient",
        "limitations": validators,
        "result_refs": [state.get("sql_hash", "")],
        "sql_hashes": [state.get("sql_hash", "")],
        "typed_payload": {
            "status": "blocked",
            "scope": state["intent"]["scope"],
            "time_window": state["intent"]["time_window"],
            "boundary_type": "validator",
            "validators": validators,
            "business_reasons": list(_business_validator_reasons(failed)),
            "repair_path": _terminal_repair_path(state, "blocked"),
        },
    }


def _blocked_validator_boundary_claim(state: WorkflowState, evidence_ref: str) -> dict[str, Any]:
    reasons = _business_validator_reasons(state.get("validator_results", ()))
    reason_text = "；".join(reasons) if reasons else "当前运行时校验未通过"
    return _with_claim_audit(
        state,
        {
            "text": f"当前主业务结论被运行时校验边界阻断；{reason_text}。",
            "evidence_refs": [evidence_ref],
            "numbers": {},
            "scope": state["intent"]["scope"],
            "time_window": state["intent"]["time_window"],
            "claim_strength": "insufficient",
        },
    )


def _blocked_coverage_claim(state: WorkflowState, evidence_ref: str) -> dict[str, Any]:
    reason_text = _coverage_block_reason_text(state.get("coverage_interpretation") or {})
    return _with_claim_audit(
        state,
        {
            "text": f"当前数据覆盖不足，无法支撑本轮业务结论；主要原因是{reason_text}。",
            "evidence_refs": [evidence_ref],
            "numbers": {},
            "scope": state["intent"]["scope"],
            "time_window": state["intent"]["time_window"],
            "claim_strength": "insufficient",
        },
    )


def _coverage_block_reason_text(coverage: Mapping[str, Any]) -> str:
    business_impact = str(coverage.get("business_impact") or "").strip()
    if business_impact:
        return business_impact.rstrip("。")
    local_reason = str(coverage.get("local_block_reason") or "")
    reason = _business_limitation_reasons((local_reason,))
    if reason:
        return reason[0]
    return "当前数据覆盖不足"


def _final_business_summary(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "final_business_summary")
    summary_payload = _final_business_summary_payload(state)
    try:
        output = _invoke_llm(
            state,
            "final_business_summary",
            summary_payload,
        )
        _apply_final_business_summary_output(state, output)
    except WorkflowFailure as exc:
        authority_record = _prepublication_narrative_authority_record(state)
        _apply_authority_safe_final_summary(
            state,
            authority_record=authority_record,
            summary_payload=summary_payload,
            reason=f"final_business_summary_provider_failed:{exc}",
        )
        return state
    if not str(state.get("final_business_summary") or "").strip():
        raise WorkflowFailure(
            "final_business_summary_contract_invalid:summary_text",
            failure_type="llm_contract",
        )
    authority_record = _prepublication_narrative_authority_record(state)
    binding_payload = {
        "frozenSummary": state["final_business_summary"],
        "businessContext": {
            **summary_payload["businessContext"],
            "publicationAuthority": _narrative_authority_catalog(
                authority_record
            ),
        },
    }
    try:
        binding_output = _invoke_llm(
            state,
            "final_narrative_binding",
            binding_payload,
            output_validator=lambda candidate: (
                _validate_final_narrative_binding_provider_output(
                    candidate,
                    binding_payload,
                )
            ),
        )
        _validate_final_narrative_binding_provider_output(
            binding_output,
            binding_payload,
        )
    except (WorkflowFailure, LLMOutputError) as exc:
        _apply_authority_safe_final_summary(
            state,
            authority_record=authority_record,
            summary_payload=summary_payload,
            reason=f"final_narrative_binding_provider_failed:{exc}",
        )
        return state
    _apply_final_narrative_binding_output(state, binding_output)
    return state


def _answer_quality_gate(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "answer_quality_gate")
    state["follow_up_questions"] = _follow_up_questions(state)
    quality_gate = evaluate_answer_quality(
        user_question=str(state.get("request", {}).get("question") or ""),
        verified_claims=_verified_claims(state),
        final_answer=state.get("final_business_summary") or state.get("answer_text", ""),
        follow_up_questions=state["follow_up_questions"],
    )
    if state.get("final_summary_display_warnings"):
        quality_gate = {
            **quality_gate,
            "final_summary_display_warnings": state["final_summary_display_warnings"],
        }
    if state.get("final_explanation") and not state.get("draft_claims"):
        quality_gate = {
            **quality_gate,
            "has_verified_claims": False,
            "verified_claim_preserved": True,
            "business_insight_present": True,
            "issues": [
                issue
                for issue in quality_gate.get("issues", [])
                if issue not in {"missing_verified_claim", "missing_business_insight"}
            ],
        }

    try:
        final_answer_audit = _with_local_final_summary_repair_warnings(
            _final_answer_audit(state),
            state,
        )
    except WorkflowFailure:
        hard_blockers = _local_final_answer_hard_blockers(state)
        final_answer_audit = {
            "display_status": (
                "hard_blocked" if hard_blockers else "ready_with_warnings"
            ),
            "hard_blockers": hard_blockers,
            "repairable_warnings": [],
            "risk_flags": ["final_answer_audit_unavailable"],
            "retry_instruction": "",
            "business_audit_summary": (
                "最终表达审阅本轮暂不可用，不影响已验证结论的展示。"
            ),
            "blocks_display": bool(hard_blockers),
        }

    state["final_answer_audit"] = final_answer_audit
    state["quality_gate"] = {
        **quality_gate,
        "display_status": final_answer_audit["display_status"],
        "hard_blockers": list(final_answer_audit["hard_blockers"]),
        "repairable_warnings": list(final_answer_audit["repairable_warnings"]),
        "risk_flags": list(final_answer_audit.get("risk_flags") or ()),
        "retry_instruction": final_answer_audit["retry_instruction"],
        "business_audit_summary": final_answer_audit["business_audit_summary"],
        "issues": [
            *list(quality_gate.get("issues", [])),
            *list(final_answer_audit["hard_blockers"]),
            *list(final_answer_audit["repairable_warnings"]),
        ],
        "blocks_display": final_answer_audit["blocks_display"],
        "final_summary_display_warnings": list(
            state.get("final_summary_display_warnings", ())
        ),
    }
    return state


def _persist_artifact(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "persist_artifact")
    request = state.get("request", {})
    analysis_runtime = request.get("analysis_runtime")
    if analysis_runtime is None:
        raise WorkflowFailure(
            "analysis_runtime_required_for_live_publication",
            failure_type="contract",
        )
    package = (
        _delivery_reverify_with_answer_repair(state)
        if analysis_runtime is not None
        else _build_answer_package_from_state(state)
    )
    artifact_path = persist_artifact(
        package,
        artifact_root=state["request"].get("artifact_root", "artifacts/phase-4"),
    )
    state["answer_package"] = package
    state["artifact_path"] = artifact_path
    return state


def _delivery_reverify_with_answer_repair(state: WorkflowState) -> dict[str, Any]:
    request = state.get("request", {})

    def verify(package: Mapping[str, Any]) -> dict[str, Any]:
        return reverify_answer_package_for_delivery(
            package,
            evidence_resolver=request.get("evidence_resolver"),
            rows_loader=request.get("rows_loader"),
            runtime_registry=request.get("runtime_registry"),
            release_resolver=request.get("release_resolver"),
        )

    authority_package = _build_answer_package_from_state(state)
    delivered = verify(authority_package)
    if str(delivered.get("status") or "") == "failed":
        errors = tuple(
            str(item.get("code") or "")
            for item in delivered.get("admin_audit", {}).get("verifier", {}).get("errors", ())
            if isinstance(item, Mapping) and item.get("code")
        )
        state["verifier"] = delivered.get("admin_audit", {}).get("verifier", {})
        state["retry_context"] = _retry_context(
            "delivery_reverify",
            "verifier",
            [{"code": code} for code in errors],
        )
        state["workflow_status"] = "failed"
        state["workflow_failure_reason"] = (
            "delivery_reverify_failed"
            + (f":{','.join(errors)}" if errors else "")
        )
        return delivered
    return authority_package


def _route_after_clarification_policy(state: WorkflowState) -> str:
    if state["clarification_outcome"].get("requires_rebind"):
        return "rebind"
    status = state["clarification_outcome"].get("boundary_status")
    if status == "needs_question":
        return "ask"
    if status == "cannot_answer":
        return "block"
    return "confirm"


def _route_after_clarification(state: WorkflowState) -> str:
    if state["clarification_outcome"].get("choice"):
        return "rebind"
    if state["clarification_outcome"].get("boundary_status") == "needs_question":
        return "wait"
    return "block"


def _route_after_accept_analysis(state: WorkflowState) -> str:
    if state.get("route_material_conflicts"):
        return "ask"
    compiled = state["compiled_graph"]
    if not compiled.mutations.accepted_graph:
        requirements = state.get("analysis_route", {}).get("analysis_requirements") or {}
        material_requirements = any(
            requirements.get(key)
            for key in ("target_metrics", "context_sources", "claim_types")
        )
        if material_requirements and state.get("repair_attempts", 0) < 2:
            return "repair"
        return "block"
    if compiled.status == "rejected":
        return "repair" if state.get("repair_attempts", 0) < 2 else "block"
    if compiled.status == "degraded":
        return "accepted"
    return "accepted"


def _route_after_coverage(state: WorkflowState) -> str:
    status = state["coverage_interpretation"].get("coverage_status", "sufficient")
    if status == "needs_question":
        return "ask" if state["request"].get("allow_question_interrupt", True) else "sufficient"
    if status == "blocked":
        return "block"
    if status == "coverage_gap_but_answerable":
        return "sufficient"
    return "sufficient"


def _route_after_runtime_binding(state: WorkflowState) -> str:
    if any(not result.get("ok", True) for result in state.get("validator_results", ())):
        return "block"
    return "valid"


def _route_after_runtime_rows(state: WorkflowState) -> str:
    if any(not result.get("ok", True) for result in state.get("validator_results", ())):
        return "block"
    return "valid"


def _runtime_row_provider(request: Mapping[str, Any]) -> Any:
    return request.get("row_provider") or request.get("runtime", {}).get("row_provider")


def _local_coverage_block_reason(state: WorkflowState) -> str:
    rows = _coverage_rows_for_local_check(state)
    if not rows:
        return "no_rows"
    required_fields = tuple(state.get("request", {}).get("required_fields") or ())
    if required_fields:
        available_fields: set[str] = set()
        for row in rows:
            available_fields.update(str(field) for field in row.keys())
        missing = [field for field in required_fields if field not in available_fields]
        if missing:
            return "missing_required_fields"
    return ""


def _local_coverage_answerable_reason(state: WorkflowState) -> str:
    for result in state.get("validator_results", ()):
        if not result.get("ok", True):
            return ""
    rows = _coverage_rows_for_local_check(state)
    if not rows:
        return ""
    required_fields = tuple(state.get("request", {}).get("required_fields") or ())
    available_fields: set[str] = set()
    for row in rows:
        available_fields.update(str(field) for field in row.keys())
    if any(field not in available_fields for field in required_fields):
        return ""

    intent = state.get("intent", {})
    params = intent.get("pattern_params", {})
    family = intent.get("pattern_family")
    if family == "custom_baseline":
        group_key = params.get("group_key", "group")
        period_key = params.get("period_key", "period")
        target_group = str(params.get("target_group", "target"))
        baseline_group = str(params.get("baseline_group", "baseline"))
        groups = {str(row.get(group_key)) for row in rows if group_key in row}
        periods = {str(row.get(period_key)) for row in rows if period_key in row}
        min_periods = _as_int(params.get("min_periods")) or 1
        if {target_group, baseline_group}.issubset(groups) and len(periods) >= min_periods:
            return "本地聚合结果已经包含目标组和基准组，并满足当前对比所需字段；可以继续做有边界的业务对比。"
        return ""

    return "本地聚合结果已经包含当前分析所需字段；可以继续做有边界的业务对比。"


def _coverage_rows_for_local_check(state: WorkflowState) -> list[dict[str, Any]]:
    request = state.get("request", {})
    rows_by_intent = request.get("runtime_rows_by_intent") or {}
    if isinstance(rows_by_intent, Mapping):
        for intent in _coverage_query_intents():
            if intent in rows_by_intent:
                rows = rows_by_intent[intent]
                if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                    return list(rows)
    return []


def _coverage_query_intents() -> tuple[str, ...]:
    return (
        "daily_metric_baselines",
        "dimension_scan",
        "joint_candidate_scan",
        "clickhouse_revenue_rows",
        "data_quality_probe",
    )


def _coverage_text_requests_confirmation(coverage: Mapping[str, Any]) -> bool:
    text = " ".join(str(coverage.get(key) or "") for key in ("business_impact", "decision_summary"))
    return any(token in text for token in ("确认", "补充", "调整查询", "无法直接", "不能直接", "不可直接"))


def _diagnostic_next_route_ids(state: WorkflowState) -> tuple[str, ...]:
    portfolio = state.get("diagnostic_insights") or {}
    sufficiency = (
        portfolio.get("diagnostic_sufficiency")
        if isinstance(portfolio, Mapping)
        else {}
    ) or {}
    raw_routes = sufficiency.get("next_routes") or ()
    if isinstance(raw_routes, (str, bytes)):
        raw_routes = (raw_routes,)
    route_ids: list[str] = []
    for item in raw_routes:
        if isinstance(item, Mapping):
            route_id = str(
                item.get("route_id")
                or item.get("candidate_id")
                or item.get("route_kind")
                or ""
            ).strip()
        else:
            route_id = str(item or "").strip()
        if route_id and route_id not in route_ids:
            route_ids.append(route_id)
    return tuple(route_ids)


def _pending_diagnostic_route_ids(state: WorkflowState) -> tuple[str, ...]:
    executed = {
        str(item)
        for item in state.get("diagnostic_route_history") or ()
        if str(item)
    }
    return tuple(
        route_id
        for route_id in _diagnostic_next_route_ids(state)
        if route_id not in executed
    )


def _route_after_next_action(state: WorkflowState) -> str:
    portfolio = state.get("diagnostic_insights") or {}
    sufficiency = (
        portfolio.get("diagnostic_sufficiency")
        if isinstance(portfolio, Mapping)
        else {}
    ) or {}
    pending_diagnostic_routes = _pending_diagnostic_route_ids(state)
    diagnostic_status = str(
        sufficiency.get("status") or sufficiency.get("decision") or ""
    )
    if diagnostic_status == "continue" and pending_diagnostic_routes:
        selected_route = pending_diagnostic_routes[0]
        state["diagnostic_route_history"] = [
            *list(state.get("diagnostic_route_history") or ()),
            selected_route,
        ]
        state["next_action"] = {
            **state.get("next_action", {}),
            "next_action": "continue_evidence",
            "diagnostic_route": selected_route,
            "decision_summary": (
                "主要贡献仍有可执行的下钻路线，继续补充能够改变业务判断的证据。"
            ),
        }
        event = _current_event(state)
        event["route"] = "diagnostic_insufficiency_continue"
        event["diagnostic_route"] = selected_route
        return "plan"

    action = state["next_action"].get("next_action", "synthesize_answer")
    if action in {"continue_evidence", "scan_sibling"}:
        route_seed = str(
            state.get("next_action", {}).get("diagnostic_route")
            or state.get("next_action", {}).get("decision_summary")
            or action
        ).strip()
        route_id = f"llm:{route_seed}"
        route_history = list(state.get("diagnostic_route_history") or ())
        if route_id not in route_history:
            state["diagnostic_route_history"] = [*route_history, route_id]
            event = _current_event(state)
            event["route"] = "plan"
            event["diagnostic_route"] = route_id
            return "plan"
        _current_event(state)["route"] = "synthesize_after_route_exhaustion"
        return "synthesize"
    if action == "ask_question":
        if _evidence_supports_bounded_answer(state):
            _current_event(state)["route"] = "ask_overridden_to_bounded_answer"
            return "synthesize"
        if _pattern_has_negative_answer_evidence(state):
            state["next_action"] = {
                **state.get("next_action", {}),
                "next_action": "synthesize_answer",
                "decision_summary": (
                    "当前数据足以回答这个假设，但证据不支持目标模式；"
                    "进入答案合成，输出不支持的业务结论和限制项。"
                ),
            }
            _current_event(state)["route"] = "ask_overridden_to_negative_answer"
            return "synthesize"
        if _evidence_has_terminal_business_boundary(state):
            state["next_action"] = {
                **state.get("next_action", {}),
                "next_action": "degrade",
                "decision_summary": (
                    "当前证据已经给出结论边界，不通过改口径强化结论；"
                    "进入降级说明，保留可见限制项。"
                ),
            }
            _current_event(state)["route"] = "ask_overridden_to_degrade"
            return "degrade"
        if state["request"].get("allow_question_interrupt", True):
            state["clarification_outcome"] = {
                **state.get("clarification_outcome", {}),
                "boundary_status": "needs_question",
                "status": "pending",
                "recommended_assumption": state.get("clarification_outcome", {}).get(
                    "recommended_assumption"
                ),
            }
            return "ask"
        return "synthesize" if _evidence_supports_bounded_answer(state) else "degrade"
    if action == "promote_attribution":
        return "promote"
    if action == "degrade":
        if _evidence_supports_bounded_answer(state):
            state["next_action"] = {
                **state.get("next_action", {}),
                "next_action": "synthesize_answer",
                "decision_summary": (
                    "已有可发布的有边界业务证据，不能把可回答结果降级为无法回答；"
                    "进入答案合成，并在答案中保留证据限制。"
                ),
                "display_summary": (
                    "已有可回答的业务证据，本轮继续生成带边界说明的答案。"
                ),
            }
            _current_event(state)["route"] = "degrade_overridden_to_bounded_answer"
            return "synthesize"
        if _pattern_has_negative_answer_evidence(state):
            state["next_action"] = {
                **state.get("next_action", {}),
                "next_action": "synthesize_answer",
                "decision_summary": (
                    "当前数据足以回答这个假设，但证据不支持目标模式；"
                    "进入答案合成，输出不支持的业务结论和限制项。"
                ),
            }
            _current_event(state)["route"] = "degrade_overridden_to_negative_answer"
            return "synthesize"
        return "degrade"
    if (
        action == "synthesize_answer"
        and _evidence_supports_bounded_answer(state)
        and _next_action_text_conflicts_with_established_evidence(state)
    ):
        state["next_action"] = {
            **state.get("next_action", {}),
            "decision_summary": (
                "已有可发布的有边界业务证据，继续生成答案；"
                "答案会同时说明归因结果和证据限制。"
            ),
            "display_summary": "已有可回答的业务证据，本轮继续生成带边界说明的答案。",
        }
        _current_event(state)["route"] = "synthesize_action_text_repaired"
    return (
        "synthesize"
        if _evidence_supports_bounded_answer(state)
        or _pattern_has_negative_answer_evidence(state)
        else "degrade"
    )


def _route_after_promotion_policy(state: WorkflowState) -> str:
    route = _current_event(state).get("route")
    if route == "accepted":
        return "accepted"
    return "synthesize" if _evidence_supports_bounded_answer(state) else "degrade"


def _next_action_text_conflicts_with_established_evidence(state: WorkflowState) -> bool:
    text = " ".join(
        str(state.get("next_action", {}).get(key) or "")
        for key in ("decision_summary", "display_summary")
    )
    if not text:
        return False
    conflict_tokens = (
        "无法执行",
        "无法完成",
        "无法分析",
        "无法回答",
        "证据不足",
        "数据不足",
        "缺少渠道字段",
        "渠道数据缺失",
        "数据缺失",
    )
    return any(token in text for token in conflict_tokens)


def _route_after_hard_verify(state: WorkflowState) -> str:
    verifier = state.get("verifier", {})
    if not verifier.get("errors"):
        return "passed"
    if verifier.get("global_errors"):
        return "degrade"
    if state.get("verifier_repair_attempts", 0) < 1:
        return "repair"
    if (
        verifier.get("status") == "degraded"
        and verifier.get("accepted_claim_indexes")
    ):
        return "passed"
    return "degrade"


def _route_after_semantic_audit(state: WorkflowState) -> str:
    audit = state.get("semantic_audit", {})
    if _semantic_audit_requires_revision(audit):
        if state.get("semantic_repair_attempts", 0) < 1:
            return "repair"
        return "verify"
    return "verify"


def _semantic_audit_requires_revision(audit: Mapping[str, Any]) -> bool:
    status = str(audit.get("audit_status") or "").strip().lower()
    issues = tuple(
        issue
        for issue in audit.get("issues") or ()
        if isinstance(issue, Mapping)
    )
    severities = tuple(
        str(issue.get("severity") or "").strip().lower()
        for issue in issues
    )
    if any(
        severity in {"error", "critical", "blocking"}
        for severity in severities
    ):
        return True
    if issues and all(
        severity in {"info", "warning"}
        for severity in severities
    ):
        return False
    return status in {"fail", "failed", "needs_revision"}


def _normalize_semantic_audit_decision(
    audit: Mapping[str, Any],
) -> dict[str, Any]:
    provider_status = str(audit.get("audit_status") or "").strip()
    issues = [
        dict(issue)
        for issue in audit.get("issues") or ()
        if isinstance(issue, Mapping)
    ]
    normalized = {
        "audit_status": provider_status,
        "provider_audit_status": provider_status,
        "issues": issues,
    }
    if provider_status.lower() in {"fail", "failed", "needs_revision"} and not (
        _semantic_audit_requires_revision(audit)
    ):
        normalized["audit_status"] = "passed"
    if _semantic_audit_requires_revision(normalized):
        normalized["display_summary"] = (
            f"答案有{len(issues)}处表述需要按当前业务证据修正。"
        )
    else:
        normalized["display_summary"] = (
            "答案与当前业务证据一致，可以进入下一步。"
        )
    return normalized


def _retry_context(failed_node: str, failure_type: str, reason: Any) -> dict[str, Any]:
    return {
        "failed_node": failed_node,
        "failure_type": failure_type,
        "failure_reason": _compact_failure_reason(reason),
    }


def _compact_failure_reason(reason: Any) -> str:
    text = json.dumps(to_jsonable(reason), ensure_ascii=False, sort_keys=True)
    return text[:2000]


def _row_query_intent(query_id: str, reason: str) -> str:
    if query_id and ":" in query_id:
        return str(query_id).rsplit(":", 1)[-1]
    return str(reason or "")


def _is_timeout_failure(exc: Exception) -> bool:
    return "timeout" in str(exc).lower()


def _authority_accepted_degradation_choice(
    state: Mapping[str, Any],
) -> dict[str, Any]:
    request = state.get("request")
    request = request if isinstance(request, Mapping) else {}
    resume = request.get("clarification_attempt_context")
    resume = resume if isinstance(resume, Mapping) else {}
    context_manifest = request.get("context_manifest")
    context_manifest = (
        context_manifest if isinstance(context_manifest, Mapping) else {}
    )
    state_assumption = next(
        (
            item
            for item in state.get("accepted_assumptions") or ()
            if isinstance(item, Mapping) and item
        ),
        {},
    )
    manifest_assumption = next(
        (
            item
            for item in context_manifest.get("accepted_assumptions") or ()
            if isinstance(item, Mapping) and item
        ),
        {},
    )
    for candidate in (
        state_assumption,
        request.get("accepted_degradation_choice"),
        resume.get("accepted_degradation_choice"),
        manifest_assumption,
    ):
        if isinstance(candidate, Mapping) and candidate:
            return dict(candidate)
    return {}


def _build_answer_package_from_state(state: WorkflowState) -> dict[str, Any]:
    compiled = state.get("compiled_graph")
    proposed_graph = compiled.mutations.proposed_graph if compiled else ()
    accepted_graph = compiled.mutations.accepted_graph if compiled else ()
    records = compiled.mutations.records if compiled else ()
    request = state.get("request", {})
    context_manifest = request.get("context_manifest") or {}
    artifact_path = str(
        Path(request.get("artifact_root", "artifacts/phase-4"))
        / state["run_id"]
        / "answer_package.json"
    )
    accepted_degradation_choice = _authority_accepted_degradation_choice(state)
    accepted_assumptions = (
        (accepted_degradation_choice,) if accepted_degradation_choice else ()
    )
    compiler_runtime_plan = dict(request.get("compiler_runtime_plan") or {})
    compiler_runtime_plan["graph_metadata"] = {
        **dict(compiler_runtime_plan.get("graph_metadata") or {}),
        "accepted_assumptions": list(accepted_assumptions),
    }
    context_request = {**request, "run_id": state["run_id"]}
    runtime_reuse_decisions = tuple(
        item
        for item in request.get("reuse_decisions") or ()
        if isinstance(item, Mapping)
        and "schema_version" in item
    )
    context_request["reuse_decisions"] = list(runtime_reuse_decisions)
    build_context = AnswerPackageBuildContext.create(
        request=context_request,
        artifact_path=artifact_path,
    )
    contract_gap_diagnostics = state.get("contract_gap_diagnostics")
    if contract_gap_diagnostics is None:
        contract_gap_diagnostics = _contract_gap_diagnostics_from_state(state)
        state["contract_gap_diagnostics"] = contract_gap_diagnostics
    intent = state.get("intent") or {}
    claim_intent_resolution = {
        "schema_version": "claim_type_resolution.v1",
        "required_claim_intents": list(
            intent.get("required_claim_types") or ()
        ),
        "auxiliary_claim_intents": list(
            intent.get("auxiliary_claim_types") or ()
        ),
    }
    claims_for_publication = (
        _verified_claims(state)
        if "authority_verified_claims" in state
        else [
            dict(item)
            for item in state.get("draft_claims") or ()
            if isinstance(item, Mapping)
        ]
    )
    return build_answer_package(
        run_id=state["run_id"],
        draft_claims=claims_for_publication,
        evidence=state.get("evidence", []),
        evidence_resolver=request.get("evidence_resolver"),
        rows_loader=request.get("rows_loader"),
        runtime_registry=request.get("runtime_registry"),
        release_resolver=request.get("release_resolver"),
        checkpoint_events=state["checkpoint_events"],
        proposed_graph=proposed_graph,
        accepted_graph=accepted_graph,
        rejected_or_degraded_mutations=records,
        validator_results=state.get("validator_results", []),
        sql_text=state.get("sql_text", ""),
        sql_hash=state.get("sql_hash", ""),
        artifact_audit={
            "path": "answer_package.json",
            "draft_only": True,
            "workflow_reference": "docs/phase-4-agent-workflow-reference.md",
            "final_summary_publication_repair": dict(
                state.get("final_summary_publication_repair") or {}
            ),
        },
        llm_calls=state.get("llm_calls", []),
        semantic_audit=state.get("semantic_audit", {}),
        final_explanation=state.get("final_explanation", {}),
        answer_text=state.get("answer_text", ""),
        final_business_summary=state.get("final_business_summary", ""),
        coverage_interpretation=state.get("coverage_interpretation", {}),
        clarification_outcome=state.get("clarification_outcome", {}),
        causal_audit=state.get("causal_audit", {}),
        causal_evidence_dossier=state.get("causal_evidence_dossier", {}),
        # Conversation context is input selection state. Verified claim context
        # is rebuilt from accepted authority evidence inside Answer Package.
        context_manifest_ref="",
        context_manifest=dict(build_context.context_owner),
        trusted_claim_provenance_record=dict(build_context.trusted_provenance),
        reuse_decisions=runtime_reuse_decisions,
        quality_gate=state.get("quality_gate", {}),
        follow_up_questions=state.get("follow_up_questions", ()),
        compiler_runtime_plan=compiler_runtime_plan,
        contract_gap_diagnostics=contract_gap_diagnostics,
        row_query_plan=state.get("row_query_plan", {}),
        snapshot_id=str(context_manifest.get("snapshot_version") or request.get("snapshot_id") or ""),
        analysis_contract=request.get("analysis_contract"),
        query_contracts=request.get("query_contracts") or (),
        query_results=request.get("query_results") or (),
        completeness_reports=request.get("completeness_reports") or (),
        capability_execution_plans=request.get("capability_execution_plans") or (),
        repair_attempts=request.get("persistence_repair_records")
        or request.get("repair_decisions")
        or state.get("query_repair_decisions")
        or (),
        available_evidence_brief=state.get("available_evidence_brief") or {},
        claim_intent_resolution=claim_intent_resolution,
        accepted_degradation_choice=accepted_degradation_choice,
        context_assumptions=accepted_assumptions,
        narrative_statement_bindings=(
            state.get("final_narrative_statement_bindings")
            if str(state.get("final_business_summary") or "").strip()
            else None
        ),
    )


def _contract_gap_diagnostics_from_state(
    state: WorkflowState,
) -> tuple[dict[str, Any], ...]:
    request = state.get("request", {})
    plan = request.get("compiler_runtime_plan")
    if not isinstance(plan, Mapping):
        return ()
    row_shapes = plan.get("row_shapes") or ()
    if not isinstance(row_shapes, Sequence) or isinstance(row_shapes, (str, bytes)):
        return ()

    contract_gaps: list[Any] = []
    for row_shape in row_shapes:
        if not isinstance(row_shape, Mapping):
            continue
        gaps = row_shape.get("contract_gaps") or ()
        if isinstance(gaps, Sequence) and not isinstance(gaps, (str, bytes)):
            contract_gaps.extend(gap for gap in gaps if gap)
    if not contract_gaps:
        return ()

    available_fields = _available_fields_for_contract_diagnostics(state)
    contract_fields = _contract_fields_for_contract_diagnostics(request)
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    return diagnose_contract_gaps(
        contract_gaps=tuple(contract_gaps),
        available_fields=available_fields,
        contract_fields=contract_fields,
        restricted_output_fields=registry.restricted_output_fields,
        unsupported_grains=request.get("unsupported_grains", ()),
    )


def _refresh_contract_gap_diagnostics(
    state: WorkflowState,
) -> tuple[dict[str, Any], ...]:
    diagnostics = _contract_gap_diagnostics_from_state(state)
    state["contract_gap_diagnostics"] = diagnostics
    return diagnostics


def _available_fields_for_contract_diagnostics(state: WorkflowState) -> tuple[str, ...]:
    request = state.get("request", {})
    available_fields: list[str] = []
    for source in (
        request.get("available_fields"),
        request.get("schema_fields"),
        request.get("clickhouse_schema_fields"),
    ):
        if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
            for field in source:
                value = str(field)
                if value and value not in available_fields:
                    available_fields.append(value)
    schema = state.get("schema") or {}
    schema_fields = schema.get("fields") or ()
    if isinstance(schema_fields, Sequence) and not isinstance(schema_fields, (str, bytes)):
        for field in schema_fields:
            value = str(field)
            if value and value not in available_fields:
                available_fields.append(value)
    plan = request.get("compiler_runtime_plan") or {}
    if isinstance(plan, Mapping):
        for source in (
            plan.get("schema_fields"),
            (plan.get("schema") or {}).get("fields")
            if isinstance(plan.get("schema"), Mapping)
            else (),
        ):
            if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
                for field in source:
                    value = str(field)
                    if value and value not in available_fields:
                        available_fields.append(value)
        row_shapes = plan.get("row_shapes") or ()
        if isinstance(row_shapes, Sequence) and not isinstance(row_shapes, (str, bytes)):
            for row_shape in row_shapes:
                if not isinstance(row_shape, Mapping):
                    continue
                source = row_shape.get("schema_fields") or ()
                if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
                    for field in source:
                        value = str(field)
                        if value and value not in available_fields:
                            available_fields.append(value)
    return tuple(available_fields)


def _contract_fields_for_contract_diagnostics(
    request: Mapping[str, Any],
) -> tuple[str, ...]:
    explicit = request.get("contract_fields") or ()
    if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
        return tuple(str(field) for field in explicit if field)
    return contract_fields_from_records(request.get("contract_registry_records"))


def evaluate_answer_quality(
    *,
    user_question: str,
    verified_claims: Sequence[Mapping[str, Any]],
    final_answer: str,
    follow_up_questions: Sequence[str],
) -> dict[str, Any]:
    answer = str(final_answer or "")
    issues: list[str] = []
    direct_answer = bool(answer.strip()) and any(
        marker in answer for marker in ("最终结论", "结论", "当前证据")
    )
    if not direct_answer:
        issues.append("missing_direct_answer")

    has_verified_claims = bool(verified_claims)
    verified_claim_preserved = has_verified_claims
    for claim in verified_claims:
        if not _verified_claim_preserved_in_answer(claim, answer):
            verified_claim_preserved = False
            break
    if not has_verified_claims:
        issues.append("missing_verified_claim")
    elif not verified_claim_preserved:
        issues.append("missing_verified_claim")

    business_insight_present = any(
        marker in answer
        for marker in (
            "当前证据能把排查方向收敛到",
            "排查方向",
            "下一步最值得",
            "洞察",
            "能把",
            "能定位",
            "能排除",
            "下一步最小",
            "优先检查",
        )
    )
    if not business_insight_present:
        issues.append("missing_business_insight")

    followups_one_intent = (
        len(follow_up_questions) == 3
        and all("以及" not in question and str(question).count("，") <= 2 for question in follow_up_questions)
    )
    if not followups_one_intent:
        issues.append("followups_not_single_intent")

    return {
        "direct_answer": direct_answer,
        "has_verified_claims": has_verified_claims,
        "verified_claim_preserved": verified_claim_preserved,
        "business_insight_present": business_insight_present,
        "followups_one_intent": followups_one_intent,
        "issues": issues,
    }


def _verified_claim_preserved_in_answer(claim: Mapping[str, Any], answer: str) -> bool:
    text = str(claim.get("text") or "").strip()
    if text and text in answer:
        return True
    if _insufficient_claim_preserved_in_answer(claim, answer):
        return True
    numbers = claim.get("numbers")
    if not isinstance(numbers, Mapping) or not numbers:
        return False
    return all(_number_value_present(value, answer) for value in numbers.values())


def _insufficient_claim_preserved_in_answer(claim: Mapping[str, Any], answer: str) -> bool:
    strength = str(claim.get("claim_strength") or "").lower()
    if strength not in {"insufficient", "degraded", "blocked"}:
        return False
    if not any(
        marker in answer
        for marker in (
            "证据不足",
            "证据不充分",
            "证据强度不足",
            "无法支持",
            "无法支撑",
            "不能支撑",
            "不足以支撑",
            "无法确认",
            "无法判断",
            "不能发布主业务结论",
            "无法得出",
            "无法进行归因",
            "无法评估",
        )
    ):
        return False
    limitation_terms = _insufficient_claim_limitation_terms(claim)
    if not limitation_terms:
        return True
    if any(term in answer for term in limitation_terms):
        return True
    non_generic_terms = [
        term for term in limitation_terms if not _generic_insufficient_limitation_term(term)
    ]
    return not non_generic_terms


def _insufficient_claim_limitation_terms(claim: Mapping[str, Any]) -> tuple[str, ...]:
    text_terms = _insufficient_claim_text_limitation_terms(str(claim.get("text") or ""))
    if text_terms:
        return text_terms

    explicit = claim.get("limitations")
    if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
        terms = [str(item).strip() for item in explicit if str(item).strip()]
        if terms:
            return tuple(terms)

    return ()


def _insufficient_claim_text_limitation_terms(text: str) -> tuple[str, ...]:
    for marker in ("主要限制是", "主要限制包括", "限制是", "限制包括"):
        if marker not in text:
            continue
        tail = text.split(marker, 1)[1]
        tail = re.split(r"[。；;]", tail, 1)[0]
        terms = [item.strip() for item in re.split(r"[、,，]", tail) if item.strip()]
        return tuple(term for term in terms if len(term) >= 2)
    return ()


def _generic_insufficient_limitation_term(term: str) -> bool:
    return "证据" in term and any(marker in term for marker in ("不足", "不充分", "不够"))


def _number_value_present(value: Any, answer: str) -> bool:
    candidates = {
        str(value),
        _format_number(value),
        _format_percent(value),
    }
    numeric = _as_float(value)
    if numeric is not None and abs(numeric) >= 1:
        candidates.add(f"{numeric:.0f}")
    return any(candidate and candidate != "unknown" and candidate in answer for candidate in candidates)


def _verified_claims(state: WorkflowState) -> list[dict[str, Any]]:
    if "authority_verified_claims" in state:
        claims = [
            dict(claim)
            for claim in state.get("authority_verified_claims") or ()
            if isinstance(claim, Mapping)
        ]
        expected_digest = str(
            state.get("authority_verified_claims_digest") or ""
        )
        if expected_digest and canonical_digest(claims) != expected_digest:
            raise WorkflowFailure(
                "authority_verified_claims_mutated",
                failure_type="verification",
            )
        return claims
    if state.get("verifier", {}).get("errors"):
        return []
    return [dict(claim) for claim in state.get("draft_claims", []) if isinstance(claim, Mapping)]


def _follow_up_questions(state: WorkflowState) -> list[str]:
    accepted = tuple(
        state.get("compiled_graph").mutations.accepted_graph
        if state.get("compiled_graph")
        else ()
    )
    if "outlier_contribution" in accepted:
        return [
            "要复核移除异常日期后的贡献变化吗？",
            "要看异常日期集中在哪些业务窗口吗？",
            "要继续检查渠道贡献是否稳定吗？",
        ]
    if "segment_contribution" in accepted or "joint_attribution" in accepted:
        return [
            "要先看哪个渠道的贡献最稳定吗？",
            "要复核异常日期剔除后的方向吗？",
            "要把新老用户贡献单独拆开看吗？",
        ]
    return [
        "要继续看贡献最大的业务因素吗？",
        "要复核异常日期对结果的影响吗？",
        "要换成日均口径再算一次吗？",
    ]


def normalize_final_answer_audit(output: Mapping[str, Any]) -> dict[str, Any]:
    material_findings = [
        dict(item)
        for item in output.get("material_findings") or ()
        if isinstance(item, Mapping)
    ]
    warnings = list(
        dict.fromkeys(
            str(item.get("code") or "")
            for item in material_findings
            if str(item.get("code") or "")
        )
    )
    if material_findings:
        business_summary = (
            f"答案有{len(material_findings)}处表述超出当前业务证据，"
            "需要按已验证事实修正。"
        )
    else:
        business_summary = "答案与当前业务证据一致，可以保留。"
    return {
        "display_status": (
            "ready_with_warnings" if material_findings else "ready"
        ),
        "blocks_display": False,
        "hard_blockers": [],
        "risk_flags": [],
        "repairable_warnings": warnings,
        "retry_instruction": _final_answer_audit_retry_instruction(
            material_findings
        ),
        "business_audit_summary": business_summary,
        "display_summary": business_summary,
        "material_findings": material_findings,
    }


def _final_answer_audit_retry_instruction(
    findings: Sequence[Mapping[str, Any]],
) -> str:
    if not findings:
        return ""
    action_labels = {
        "remove": "删除",
        "weaken": "弱化",
        "clarify": "澄清",
    }
    instructions = []
    for finding in findings:
        excerpt = str(finding.get("answer_excerpt") or "").strip()
        action = action_labels.get(
            str(finding.get("edit_action") or ""),
            "修正",
        )
        instructions.append(f"{action}“{excerpt}”")
    return "请仅处理以下已定位表达：" + "；".join(instructions) + "。"


def _final_business_summary_payload(
    state: WorkflowState,
) -> dict[str, Any]:
    contract_gap_diagnostics = _refresh_contract_gap_diagnostics(state)
    available_evidence_brief = build_available_evidence_brief(
        verified_claims=_verified_claims_for_available_evidence_brief(state),
        capability_bindings=state.get("capability_bindings")
        or state.get("request", {}).get("capability_bindings")
        or state.get("request", {}).get("capability_execution_plans")
        or (),
        contract_gaps=contract_gap_diagnostics,
        obligation_resolution=state.get("analysis_route", {}).get(
            "obligation_resolution", {}
        ),
    )
    state["available_evidence_brief"] = available_evidence_brief
    return {
        "draftAnswer": state.get("answer_text", ""),
        "businessContext": _business_answer_context(state),
        "displayReview": _business_display_review(state, stage="final"),
    }


def _verified_claims_for_available_evidence_brief(
    state: WorkflowState,
) -> tuple[dict[str, Any], ...]:
    if "authority_verified_claims" in state:
        return tuple(_verified_claims(state))
    if state.get("verifier", {}).get("errors"):
        return ()
    if not state.get("run_id") or "checkpoint_events" not in state:
        return ()
    package = _build_answer_package_from_state(state)
    verifier = package.get("admin_audit", {}).get("verifier", {})
    if verifier.get("status") not in {
        "passed",
        "passed_with_warnings",
        "degraded",
    }:
        return ()
    return tuple(
        dict(item)
        for item in package.get("admin_audit", {}).get("verified_claims", ())
        if isinstance(item, Mapping)
    )


def build_available_evidence_brief(
    *,
    verified_claims: Sequence[Mapping[str, Any]],
    capability_bindings: Sequence[Mapping[str, Any]],
    contract_gaps: Sequence[Mapping[str, Any]],
    obligation_resolution: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the authority-backed facts and scoped limits allowed into synthesis."""

    required_claim_fields = (
        "claim_ref",
        "context_manifest_ref",
        "evidence_refs",
        "result_refs",
        "artifact_refs",
        "memory_refs",
        "reuse_decisions",
        "provenance_record_ref",
    )
    claims = [
        dict(item)
        for item in verified_claims
        if isinstance(item, Mapping)
        and all(item.get(field) for field in required_claim_fields)
    ]
    gaps = [dict(item) for item in contract_gaps if isinstance(item, Mapping)]
    return {
        "verified_claims": claims,
        "verified_capabilities": sorted(
            {
                str(item.get("capability_id") or "")
                for item in capability_bindings
                if isinstance(item, Mapping)
                and item.get("status") in {"ready", "degraded"}
                and item.get("capability_id")
            }
        ),
        "unresolved_obligations": [
            str(item)
            for item in obligation_resolution.get("unresolved", ())
            if str(item)
        ],
        "scoped_gaps": gaps,
        "omitted_factors": list(
            dict.fromkeys(
                str(item.get("dataset_id") or item.get("gap_id") or "")
                for item in gaps
                if item.get("dataset_id") or item.get("gap_id")
            )
        ),
        "business_next_actions": sorted(
            {
                action
                for item in gaps
                for action in _contract_gap_next_actions(item)
                if action
            }
        ),
    }


def _contract_gap_next_actions(gap: Mapping[str, Any]) -> tuple[str, ...]:
    repair_path = str(gap.get("repair_path") or "").strip()
    legacy = gap.get("repair_options") or ()
    if isinstance(legacy, (str, bytes)):
        legacy = (legacy,)
    return tuple(
        dict.fromkeys(
            item
            for item in (
                repair_path,
                *(str(value).strip() for value in legacy),
            )
            if item
        )
    )


def _with_local_final_summary_repair_warnings(
    audit: Mapping[str, Any],
    state: WorkflowState,
) -> dict[str, Any]:
    merged = dict(audit)
    if merged.get("blocks_display"):
        return merged
    repairable_display_warnings = {
        "internal_visible_token",
        "missing_pattern_evidence",
        "missing_driver_claim",
        "missing_primary_claim",
    }
    local_warnings = [
        str(item)
        for item in state.get("final_summary_display_warnings", ())
        if str(item) in repairable_display_warnings
    ]
    if not local_warnings:
        return merged
    merged["repairable_warnings"] = list(
        dict.fromkeys([*list(merged.get("repairable_warnings", ())), *local_warnings])
    )
    if not str(merged.get("retry_instruction") or ""):
        merged["retry_instruction"] = "请重新输出完整中文业务答案，修复本地展示检查指出的问题。"
    if merged.get("display_status") == "ready":
        merged["display_status"] = "ready_with_warnings"
    return merged


def _apply_final_business_summary_output(
    state: WorkflowState,
    output: Mapping[str, Any],
) -> None:
    state["final_business_summary"] = _final_business_summary_text(output)
    state["final_summary_display_warnings"] = _final_summary_display_repair_reasons(
        state["final_business_summary"],
        state,
    )


def _apply_authority_safe_final_summary(
    state: WorkflowState,
    *,
    authority_record: Mapping[str, Any],
    summary_payload: Mapping[str, Any],
    reason: str,
) -> None:
    required_resolution = _required_claim_evidence_resolution(state)
    required_claim_types: tuple[str, ...] | None = tuple(
        claim_type
        for claim_type, entry in required_resolution.get("required", {}).items()
        if isinstance(entry, Mapping) and entry.get("status") == "publishable"
    )
    if not required_claim_types:
        primary_claim_type = str(
            (required_resolution.get("primary") or {}).get("claim_type") or ""
        )
        required_claim_types = (
            (primary_claim_type,) if primary_claim_type else None
        )
    projection = build_authority_safe_narrative(
        authority_record,
        required_claim_types=required_claim_types,
    )
    if projection.get("status") != "bound":
        validation_errors = ",".join(
            str(item) for item in projection.get("validation_errors") or ()
        )
        raise WorkflowFailure(
            "final_authority_safe_projection_failed:"
            + (validation_errors or "no_publishable_authority"),
            failure_type="verification",
        )

    rejected_summary = str(state.get("final_business_summary") or "").strip()
    safe_summary = str(projection.get("narrative") or "").strip()
    safe_bindings = [
        dict(item)
        for item in projection.get("statement_bindings") or ()
        if isinstance(item, Mapping)
    ]
    if not safe_summary or not safe_bindings:
        raise WorkflowFailure(
            "final_authority_safe_projection_failed:empty_projection",
            failure_type="verification",
        )

    safe_binding_payload = {
        "frozenSummary": safe_summary,
        "businessContext": {
            **dict(summary_payload.get("businessContext") or {}),
            "publicationAuthority": _narrative_authority_catalog(
                authority_record
            ),
        },
    }
    safe_binding_output = {"statement_bindings": safe_bindings}
    _validate_final_narrative_binding_provider_output(
        safe_binding_output,
        safe_binding_payload,
    )

    state["rejected_final_business_summary"] = rejected_summary
    state["final_business_summary"] = safe_summary
    state["final_narrative_statement_bindings"] = safe_bindings
    state["final_summary_display_warnings"] = (
        _final_summary_display_repair_reasons(safe_summary, state)
    )
    state["final_summary_publication_repair"] = {
        "schema_version": "final-summary-publication-repair.v1",
        "status": "authority_projected",
        "reason": reason,
        "rejected_summary_digest": (
            canonical_digest({"final_business_summary": rejected_summary})
            if rejected_summary
            else ""
        ),
        "published_summary_digest": canonical_digest(
            {"final_business_summary": safe_summary}
        ),
        "accepted_authority_keys": list(
            projection.get("accepted_authority_keys") or ()
        ),
        "required_claim_types": list(
            projection.get("required_claim_types") or ()
        ),
        "missing_required_claim_types": list(
            projection.get("missing_required_claim_types") or ()
        ),
        "omitted_authorities": list(
            projection.get("omitted_authorities") or ()
        ),
    }
    state["llm_calls"].append(
        _local_llm_decision_audit(
            task="final_business_summary",
            payload=dict(summary_payload),
            output={"summary_text": safe_summary},
            reason="final_summary_authority_projection",
        )
    )
    state["llm_calls"].append(
        _local_llm_decision_audit(
            task="final_narrative_binding",
            payload=safe_binding_payload,
            output=safe_binding_output,
            reason="final_summary_authority_projection_binding",
        )
    )


def _apply_final_narrative_binding_output(
    state: WorkflowState,
    output: Mapping[str, Any],
) -> None:
    state["final_narrative_statement_bindings"] = list(
        _final_narrative_statement_bindings(output)
    )


def _final_business_summary_text(output: Mapping[str, Any]) -> str:
    raw_text = output.get("summary_text")
    if (
        not isinstance(raw_text, str)
        or not raw_text.strip()
        or raw_text != raw_text.strip()
    ):
        raise WorkflowFailure(
            "final_business_summary_contract_invalid:summary_text",
            failure_type="llm_contract",
        )
    return raw_text


def _validate_final_narrative_statement_bindings(
    output: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> None:
    summary_text = str(payload.get("frozenSummary") or "").strip()
    if not summary_text:
        summary_text = _final_business_summary_text(output)
    bindings = _final_narrative_statement_bindings(output)
    business_context = payload.get("businessContext") or {}
    evidence = (
        business_context.get("evidence")
        if isinstance(business_context, Mapping)
        else {}
    ) or {}
    allowed_authority_keys = {
        "问题范围",
        "原因边界",
        *(
            str(item.get("authorityKey") or "")
            for item in business_context.get("publicationAuthority") or ()
            if isinstance(item, Mapping)
            and str(item.get("authorityKey") or "")
        ),
        *(
            str(item.get("claimSlot") or "")
            for item in evidence.get("claimSlots") or ()
            if isinstance(item, Mapping) and str(item.get("claimSlot") or "")
        ),
        *(
            str(item.get("factor") or "")
            for item in evidence.get("factorStates") or ()
            if isinstance(item, Mapping) and str(item.get("factor") or "")
        ),
        *(
            f"数据边界{index}"
            for index, _ in enumerate(evidence.get("boundaries") or (), start=1)
        ),
    }
    for item in bindings:
        excerpt = str(item["excerpt"])
        if excerpt not in summary_text:
            raise LLMOutputError(
                "final_business_summary_binding_excerpt_not_found"
            )
        if any(
            str(key) not in allowed_authority_keys
            for key in item["authority_keys"]
        ):
            raise LLMOutputError(
                "final_business_summary_binding_authority_unknown"
            )


def _validate_final_narrative_binding_provider_output(
    output: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> None:
    """Validate provider-owned binding shape without granting publication authority."""

    _validate_final_narrative_statement_bindings(output, payload)


def _prepublication_narrative_authority_record(
    state: WorkflowState,
) -> dict[str, Any]:
    claims = _verified_claims(state)
    prepublication_claims = tuple(
        {
            **dict(claim),
            "claim_ref": str(claim.get("claim_ref") or f"claim:prepublication:{index}"),
        }
        for index, claim in enumerate(claims, start=1)
        if isinstance(claim, Mapping)
    )
    business_evidence = _business_evidence_context(state)
    return build_narrative_authority_record(
        verified_claims=prepublication_claims,
        evidence=tuple(
            item
            for item in state.get("evidence") or ()
            if isinstance(item, Mapping)
        ),
        visible_limitations=tuple(
            business_evidence.get("boundaries") or ()
        ),
        accepted_assumptions=tuple(
            item
            for item in state.get("accepted_assumptions") or ()
            if isinstance(item, Mapping)
        ),
        question_scope=build_narrative_question_scope(
            state.get("request", {}).get("analysis_contract")
        ),
        diagnostic_insights=state.get("diagnostic_insights") or {},
    )


def _narrative_authority_catalog(
    authority_record: Mapping[str, Any],
) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for claim in authority_record.get("claims") or ():
        if isinstance(claim, Mapping) and claim.get("authority_key"):
            catalog.append(
                {
                    "authorityKey": str(claim["authority_key"]),
                    "authorityType": "业务结论",
                    "statement": str(claim.get("statement") or ""),
                    "numbers": dict(claim.get("numbers") or {}),
                }
            )
    for factor in authority_record.get("factor_states") or ():
        if isinstance(factor, Mapping) and factor.get("factor"):
            catalog.append(
                {
                    "authorityKey": str(factor["factor"]),
                    "authorityType": "因素状态",
                    "statement": str(factor.get("state") or ""),
                    "numbers": {
                        key: factor[key]
                        for key in (
                            "baseline",
                            "target",
                            "change",
                            "changeRate",
                            "contribution",
                            "contributionShare",
                        )
                        if factor.get(key) is not None
                    },
                }
            )
    for insight in authority_record.get("diagnostic_insights") or ():
        if isinstance(insight, Mapping) and insight.get("authority_key"):
            catalog.append(
                {
                    "authorityKey": str(insight["authority_key"]),
                    "authorityType": "诊断洞察",
                    "statement": str(insight.get("statement") or ""),
                    "businessLabels": list(
                        insight.get("business_labels") or ()
                    ),
                    "numbers": dict(insight.get("numbers") or {}),
                    "evidenceState": str(
                        insight.get("evidence_state") or ""
                    ),
                }
            )
    catalog.extend(
        {
            "authorityKey": f"数据边界{index}",
            "authorityType": "数据边界",
            "statement": str(limitation),
        }
        for index, limitation in enumerate(
            authority_record.get("limitations") or (),
            start=1,
        )
    )
    catalog.extend(
        (
            {
                "authorityKey": "原因边界",
                "authorityType": "原因边界",
                "statement": str(
                    authority_record.get("causal_boundary") or ""
                ),
            },
            {
                "authorityKey": "问题范围",
                "authorityType": "问题范围",
                "statement": "仅用于绑定本次已确认的指标、范围与时间窗口。",
            },
        )
    )
    return catalog


def _final_narrative_statement_bindings(
    output: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    raw = output.get("statement_bindings")
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, (str, bytes, bytearray))
    ):
        raise LLMOutputError(
            "final_business_summary_contract_invalid:statement_bindings"
        )
    allowed_classes = {
        "verified_claim",
        "factor_contribution",
        "factor_observation",
        "data_boundary",
        "analysis_scope",
        "next_check",
    }
    expected_fields = {"excerpt", "statement_class", "authority_keys"}
    normalized: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != expected_fields:
            raise LLMOutputError(
                "final_business_summary_binding_shape_invalid"
            )
        excerpt = str(item.get("excerpt") or "")
        statement_class = str(item.get("statement_class") or "")
        authority_keys = item.get("authority_keys")
        if (
            not excerpt.strip()
            or excerpt != excerpt.strip()
            or statement_class not in allowed_classes
            or not isinstance(authority_keys, Sequence)
            or isinstance(authority_keys, (str, bytes, bytearray))
            or not authority_keys
            or any(not str(key) for key in authority_keys)
        ):
            raise LLMOutputError(
                "final_business_summary_binding_value_invalid"
            )
        normalized.append(
            {
                "excerpt": excerpt,
                "statement_class": statement_class,
                "authority_keys": list(
                    dict.fromkeys(str(key) for key in authority_keys)
                ),
            }
        )
    return tuple(normalized)


def _validate_final_answer_audit_provider_output(
    output: Mapping[str, Any],
    *,
    final_answer: str,
    business_context: Mapping[str, Any],
) -> None:
    expected_top_level = {"material_findings"}
    if set(output) != expected_top_level:
        raise LLMOutputError("final_answer_audit_top_level_contract_invalid")
    findings = output.get("material_findings")
    if not isinstance(findings, list):
        raise LLMOutputError("final_answer_audit_findings_not_array")
    allowed_codes = {
        "unsupported_material_claim",
        "claim_paraphrase_drift",
        "claim_paraphrase_unclear",
    }
    allowed_actions = {"remove", "weaken", "clarify"}
    anchor_summaries = {
        (
            str(item.get("kind") or ""),
            str(item.get("key") or ""),
        ): str(item.get("summary") or "")
        for item in business_context.get("reviewAnchors") or ()
        if isinstance(item, Mapping)
        and str(item.get("kind") or "")
        and str(item.get("key") or "")
    }
    allowed_anchors = set(anchor_summaries)
    verified_fact_text = "\n".join(
        summary
        for (kind, _key), summary in anchor_summaries.items()
        if kind == "verified_fact"
    )
    expected_fields = {
        "code",
        "answer_excerpt",
        "context_anchor",
        "edit_action",
        "explanation",
    }
    explanations: list[str] = []
    for finding in findings:
        if not isinstance(finding, Mapping) or set(finding) != expected_fields:
            raise LLMOutputError("final_answer_audit_finding_shape_invalid")
        code = str(finding.get("code") or "")
        if code not in allowed_codes:
            raise LLMOutputError("final_answer_audit_finding_code_invalid")
        excerpt = str(finding.get("answer_excerpt") or "")
        if not excerpt.strip() or excerpt not in final_answer:
            raise LLMOutputError("final_answer_audit_excerpt_not_found")
        anchor = finding.get("context_anchor")
        if not isinstance(anchor, Mapping) or set(anchor) != {"kind", "key"}:
            raise LLMOutputError("final_answer_audit_context_anchor_invalid")
        anchor_key = (
            str(anchor.get("kind") or ""),
            str(anchor.get("key") or ""),
        )
        if anchor_key not in allowed_anchors:
            raise LLMOutputError("final_answer_audit_context_anchor_unknown")
        if re.search(r"导致|造成|驱动|归因于|原因是", excerpt):
            anchor_summary = anchor_summaries.get(anchor_key, "")
            if anchor_key[0] != "boundary" and not re.search(
                r"导致|造成|驱动|归因于|原因是|因果",
                anchor_summary,
            ):
                raise LLMOutputError(
                    "final_answer_audit_cause_anchor_invalid"
                )
        if str(finding.get("edit_action") or "") not in allowed_actions:
            raise LLMOutputError("final_answer_audit_edit_action_invalid")
        explanation = str(finding.get("explanation") or "").strip()
        if not explanation:
            raise LLMOutputError("final_answer_audit_explanation_missing")
        if verified_fact_text and re.search(
            r"精确|数值|数字|近似|个位",
            explanation,
        ):
            excerpt_numbers = {
                token.replace(",", "").replace(" ", "")
                for token in re.findall(r"\d[\d,]*(?:\.\d+)?%?", excerpt)
            }
            verified_numbers = {
                token.replace(",", "").replace(" ", "")
                for token in re.findall(
                    r"\d[\d,]*(?:\.\d+)?%?",
                    verified_fact_text,
                )
            }
            if excerpt_numbers and excerpt_numbers.issubset(verified_numbers):
                raise LLMOutputError(
                    "final_answer_audit_finding_contradicts_verified_value"
                )
        explanations.append(explanation)

    narrative = "\n".join(explanations)
    speculative_cause = re.compile(
        r"(?:可能(?:受|因|由|影响)|可能.{0,12}(?:带来|导致|造成|影响)"
        r"|例如|比如|譬如|说明.{0,12}(?:未造成|没有造成|导致|影响))"
    )
    source_text = final_answer + "\n" + json.dumps(
        business_context,
        ensure_ascii=False,
        sort_keys=True,
    )
    if speculative_cause.search(narrative) and not speculative_cause.search(
        source_text
    ):
        raise LLMOutputError("final_answer_audit_invents_business_cause")


def _final_answer_audit(state: WorkflowState) -> dict[str, Any]:
    final_answer = str(
        state.get("final_business_summary") or state.get("answer_text", "")
    )
    business_context = _business_final_audit_context(state)
    provider_output = _invoke_llm(
        state,
        "final_answer_audit",
        {
            "finalAnswer": final_answer,
            "businessContext": business_context,
            "displayReview": _business_display_review(state, stage="final"),
        },
        output_validator=lambda candidate: (
            _validate_final_answer_audit_provider_output(
                candidate,
                final_answer=final_answer,
                business_context=business_context,
            )
        ),
    )
    try:
        _validate_final_answer_audit_provider_output(
            provider_output,
            final_answer=final_answer,
            business_context=business_context,
        )
    except LLMOutputError as exc:
        raise WorkflowFailure(str(exc), failure_type="llm") from exc
    audit = normalize_final_answer_audit(provider_output)
    hard_blockers = list(audit["hard_blockers"])
    for blocker in _local_final_answer_hard_blockers(state):
        if blocker not in hard_blockers:
            hard_blockers.append(blocker)
    audit["hard_blockers"] = hard_blockers
    audit["blocks_display"] = audit["display_status"] == "hard_blocked" or bool(hard_blockers)
    if audit["blocks_display"]:
        audit["display_status"] = "hard_blocked"
    elif audit["repairable_warnings"] and audit["display_status"] == "ready":
        audit["display_status"] = "ready_with_warnings"
    return audit


def _final_answer_audit_evidence_envelopes(state: WorkflowState) -> list[dict[str, Any]]:
    evidence_by_ref = _evidence_by_ref(
        [
            dict(item)
            for item in state.get("evidence", ())
            if isinstance(item, Mapping) and item.get("evidence_ref")
        ]
    )
    claims = _verified_claims(state)
    refs = [
        str(ref)
        for claim in claims
        if isinstance(claim, Mapping)
        for ref in claim.get("evidence_refs", ())
    ]
    evidence_brief = state.get("evidence_brief", {})
    if isinstance(evidence_brief, Mapping):
        refs.extend(str(ref) for ref in evidence_brief.get("evidence_refs", ()))
    return [
        to_jsonable(evidence_by_ref[ref])
        for ref in dict.fromkeys(refs)
        if ref in evidence_by_ref
    ]


def _local_final_answer_hard_blockers(state: WorkflowState) -> list[str]:
    blockers: list[str] = []
    validators = state.get("validator_results", ())
    if any(
        isinstance(item, Mapping)
        and item.get("validator") == "sensitive_output_policy"
        and not item.get("ok", False)
        for item in validators
    ):
        blockers.append("sensitive_output_leak")
    if any(
        isinstance(item, Mapping)
        and item.get("validator") == "sql_safety"
        and not item.get("ok", False)
        for item in validators
    ):
        blockers.append("sql_security_failure")
    verifier = state.get("verifier", {})
    verifier_errors = verifier.get("global_errors") or ()
    if not verifier_errors and not (
        verifier.get("status") == "degraded"
        and verifier.get("accepted_claim_indexes")
    ):
        verifier_errors = verifier.get("errors") or ()
    if verifier_errors:
        blockers.append("verifier_evidence_contradiction")
        unsupported_main_claim_codes = {
            "missing_evidence_ref",
            "missing_required_claim",
            "strong_claim_without_supported_wording",
            "number_mismatch",
            "scope_mismatch",
            "time_window_mismatch",
            "window_mismatch",
        }
        if any(
            isinstance(item, Mapping) and item.get("code") in unsupported_main_claim_codes
            for item in verifier_errors
        ):
            blockers.append("unsupported_main_claim")
    return blockers


def _invoke_llm(
    state: WorkflowState,
    task: str,
    payload: dict[str, Any],
    *,
    output_validator: Callable[[Mapping[str, Any]], None] | None = None,
    defer_narrative_validation: bool = False,
) -> dict[str, Any]:
    spec = build_prompt(task, payload)
    try:
        client = state["llm_client"]
        invoke_kwargs: dict[str, Any] = {
            "task": spec.task,
            "prompt_version": spec.prompt_version,
            "messages": spec.messages,
            "required_keys": spec.required_keys,
        }
        if output_validator is not None and bool(
            getattr(client, "supports_output_validator", False)
        ):
            invoke_kwargs["output_validator"] = output_validator
        if defer_narrative_validation and bool(
            getattr(client, "supports_deferred_narrative_validation", False)
        ):
            invoke_kwargs["defer_narrative_validation"] = True
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
    except Exception as exc:
        failure_audit = getattr(exc, "audit", None)
        if isinstance(failure_audit, Mapping) and failure_audit:
            state["llm_calls"].append(dict(failure_audit))
        raise WorkflowFailure(_exception_reason(exc), failure_type="llm") from exc
    state["llm_calls"].append(result.audit)
    return result.output


def _local_llm_decision_audit(
    *,
    task: str,
    payload: dict[str, Any],
    output: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    spec = build_prompt(task, payload)
    now = _utc_now()
    return {
        "task": spec.task,
        "provider": "local_deterministic",
        "model": "contract_policy",
        "prompt_version": spec.prompt_version,
        "response_id": f"local-deterministic-{task}",
        "messages": [dict(message) for message in spec.messages],
        "required_keys": list(spec.required_keys),
        "raw_response_content": json.dumps(output, ensure_ascii=False, sort_keys=True),
        "started_at": now,
        "finished_at": now,
        "duration_ms": 0.0,
        "decision_reason": reason,
        "usage": {},
        "structured_output": output,
    }


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


def _refresh_persisted_answer_package(state: WorkflowState) -> None:
    package = state.get("answer_package")
    if not package:
        return
    package["checkpoint_events"] = to_jsonable(state.get("checkpoint_events", ()))
    state["answer_package"] = package
    persist_artifact(
        package,
        artifact_root=state["request"].get("artifact_root", "artifacts/phase-4"),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _checkpoint_summary(state: WorkflowState) -> list[dict[str, Any]]:
    return [
        {
            "label": event.get("label"),
            "status": event.get("status"),
            "route": event.get("route"),
            "llm": event.get("llm"),
        }
        for event in state.get("checkpoint_events", ())
    ]


def _current_event(state: WorkflowState) -> dict[str, Any]:
    return state["checkpoint_events"][-1]


def _maybe_force_node_failure(state: WorkflowState, node_name: str) -> None:
    forced = state["request"].get("force_failure")
    if not forced or forced.get("node") != node_name:
        return
    failure_type = forced.get("failure_type", "technical")
    raise WorkflowFailure(f"forced_{failure_type}_failure:{node_name}", failure_type=failure_type)


def _evidence_dict(item: Any, state: WorkflowState) -> dict[str, Any]:
    evidence = to_jsonable(item)
    capability_id = str(
        evidence.get("capability_id") or evidence.get("capability") or ""
    )
    run_id = str(state.get("run_id") or "")
    if capability_id and run_id:
        evidence["evidence_ref"] = f"{capability_id}:{run_id}"
    payload = dict(evidence.get("typed_payload", {}))
    payload["scope"] = state["intent"]["scope"]
    payload["time_window"] = state["intent"]["time_window"]
    evidence["typed_payload"] = payload
    evidence.setdefault("capability_id", evidence.get("capability"))
    evidence.setdefault("capability", evidence.get("capability_id"))
    evidence.setdefault(
        "numeric_facts",
        {
            key: value
            for key, value in payload.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        },
    )
    evidence.setdefault("result_refs", (state.get("sql_hash"),))
    evidence.setdefault("sql_hashes", evidence.get("result_refs", ()))
    return _apply_bound_evidence_authority(evidence, state)


def _apply_bound_evidence_authority(
    evidence: dict[str, Any],
    state: WorkflowState,
) -> dict[str, Any]:
    capability_id = str(evidence.get("capability_id") or evidence.get("capability") or "")
    bound, limitation = _production_bound_input(state, capability_id)
    if bound is None and not limitation:
        return evidence
    if limitation or bound is None:
        return _blocked_bound_evidence(
            capability_id,
            state,
            limitation or "missing_bound_capability_input",
        )

    claim_type = str(evidence.get("claim_type") or "")
    if not claim_type and len(bound.supported_claim_types) == 1:
        claim_type = bound.supported_claim_types[0]
    if claim_type not in bound.supported_claim_types:
        return _blocked_bound_evidence(
            capability_id,
            state,
            "unsupported_claim_type",
        )
    evidence_type = str(evidence.get("evidence_type") or "")
    if evidence_type not in bound.supported_evidence_types:
        evidence["limitations"] = tuple(
            dict.fromkeys((*tuple(evidence.get("limitations") or ()), "unsupported_evidence_type"))
        )
        evidence["evidence_type"] = "insufficient"
        evidence["strength"] = "low"
        evidence["wording_limit"] = "blocked"

    evidence.update(
        {
            "claim_type": claim_type,
            "analysis_contract_ref": bound.analysis_contract_ref,
            "capability_contract_ref": bound.capability_contract_ref,
            "query_contract_refs": tuple(
                dict.fromkeys(
                    (*bound.query_contract_refs, *bound.validation_query_contract_refs)
                )
            ),
            "result_refs": tuple(
                dict.fromkeys((*bound.result_refs, *bound.validation_result_refs))
            ),
            "sql_hashes": (),
            "query_execution_record_refs": tuple(
                dict.fromkeys(
                    (
                        *bound.query_execution_record_refs,
                        *bound.validation_query_execution_record_refs,
                    )
                )
            ),
            "query_execution_record_digests": tuple(
                dict.fromkeys(
                    (
                        *bound.query_execution_record_digests,
                        *bound.validation_query_execution_record_digests,
                    )
                )
            ),
            "rows_metadata_record_refs": tuple(
                dict.fromkeys(
                    (
                        *bound.rows_metadata_record_refs,
                        *bound.validation_rows_metadata_record_refs,
                    )
                )
            ),
            "rows_metadata_record_digests": tuple(
                dict.fromkeys(
                    (
                        *bound.rows_metadata_record_digests,
                        *bound.validation_rows_metadata_record_digests,
                    )
                )
            ),
            "completeness_report_refs": tuple(
                dict.fromkeys(
                    (*bound.completeness_report_refs, *bound.validation_completeness_report_refs)
                )
            ),
            "completeness_record_refs": tuple(
                dict.fromkeys(
                    (*bound.completeness_record_refs, *bound.validation_completeness_record_refs)
                )
            ),
            "completeness_record_digests": tuple(
                dict.fromkeys(
                    (
                        *bound.completeness_record_digests,
                        *bound.validation_completeness_record_digests,
                    )
                )
            ),
            "source_snapshot_refs": tuple(
                dict.fromkeys((*bound.source_snapshot_refs, *bound.validation_source_snapshot_refs))
            ),
            "supported_evidence_types": bound.supported_evidence_types,
            "supported_claim_types": bound.supported_claim_types,
            "maximum_claim_strength": bound.maximum_claim_strength,
            "maximum_claim_strength_rank": bound.maximum_claim_strength_rank,
            "claim_strength_taxonomy_version": bound.claim_strength_taxonomy_version,
            "input_status": bound.status,
            "claim_input_ready": capability_binding_claim_ready(bound),
            "input_completeness_statuses": bound.input_completeness_statuses,
            "binding_manifest_ref": bound.binding_manifest_ref,
            "binding_manifest_digest": bound.binding_manifest_digest,
        }
    )
    return evidence


def _blocked_bound_evidence(
    capability_id: str,
    state: WorkflowState,
    reason: str,
) -> dict[str, Any]:
    return {
        "evidence_ref": f"{capability_id}:{state.get('run_id', '')}:blocked",
        "capability_id": capability_id,
        "capability": capability_id,
        "claim_type": "",
        "numeric_facts": {},
        "typed_payload": {
            "status": "blocked",
            "limitation": reason,
            "scope": state.get("intent", {}).get("scope", ""),
            "time_window": state.get("intent", {}).get("time_window", ""),
        },
        "result_refs": (),
        "sql_hashes": (),
        "evidence_type": "insufficient",
        "strength": "low",
        "wording_limit": "blocked",
        "limitations": (reason,),
        "disabled_degraded_blocked_path_refs": (reason,),
        "input_status": "blocked",
        "claim_input_ready": False,
        "binding_manifest_ref": "",
        "binding_manifest_digest": "",
    }


def _evidence_by_ref(evidence: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["evidence_ref"]: item for item in evidence}


def _pattern_supports_bounded_answer(state: WorkflowState) -> bool:
    pattern = _pattern_evidence(state)
    if _evidence_established(pattern):
        return True
    return pattern.get("strength") in {"high", "medium"} and pattern.get("wording_limit") in {
        "supported",
        "candidate_mechanism_only",
    }


def _evidence_supports_bounded_answer(state: WorkflowState) -> bool:
    resolution = _required_claim_evidence_resolution(state)
    if resolution["has_required_claims"]:
        return any(
            entry.get("status") == "publishable"
            for entry in resolution["required"].values()
        )
    if _pattern_evidence(state):
        return _pattern_supports_bounded_answer(state)
    primary = _primary_business_evidence(state)
    return _evidence_established(primary)


def _evidence_has_terminal_business_boundary(state: WorkflowState) -> bool:
    limitations = set(state.get("evidence_brief", {}).get("limitations", ()))
    terminal_limitations = {
        "insufficient_comparable_periods",
        "no_comparable_periods",
        "no_rows",
        "missing_required_fields",
        "insufficient_values",
        "driver_components_missing",
    }
    if not limitations & terminal_limitations:
        return False
    if _pattern_evidence(state):
        return True
    primary = _primary_business_evidence(state)
    return bool(primary.get("evidence_ref") or primary.get("capability_id"))


def _pattern_has_negative_answer_evidence(state: WorkflowState) -> bool:
    pattern = _pattern_evidence(state)
    if not pattern:
        return False
    limitations = set(state.get("evidence_brief", {}).get("limitations", ()))
    if limitations & {"no_rows", "no_comparable_periods", "insufficient_values"}:
        return False
    try:
        comparable_periods = int(pattern.get("typed_payload", {}).get("comparable_periods", 0))
    except (TypeError, ValueError):
        comparable_periods = 0
    if comparable_periods <= 0:
        return False
    return bool(limitations & {"weak_direction", "below_materiality_floor"})


def _preserved_authority_claims(state: WorkflowState) -> list[dict[str, Any]]:
    """Keep canonical facts stable while semantic repair rewrites prose."""

    if "authority_verified_claims" in state:
        return _verified_claims(state)

    evidence_by_ref = _evidence_by_ref(state.get("evidence", []))
    preserved = []
    for raw_claim in state.get("draft_claims") or ():
        if not isinstance(raw_claim, Mapping):
            continue
        refs = tuple(str(ref) for ref in raw_claim.get("evidence_refs") or ())
        bound_authorities = tuple(
            evidence_by_ref[ref]
            for ref in refs
            if ref in evidence_by_ref
            and evidence_by_ref[ref].get("binding_manifest_ref")
        )
        authorities = tuple(
            evidence_by_ref[ref]
            for ref in refs
            if ref in evidence_by_ref
            and evidence_by_ref[ref].get("binding_manifest_ref")
            and _evidence_claim_input_ready(evidence_by_ref[ref])
        )
        if len(bound_authorities) != 1 or len(authorities) != 1:
            continue
        authority = authorities[0]
        claim_type = str(raw_claim.get("claim_type") or "")
        claim_strength = str(raw_claim.get("claim_strength") or "")
        if claim_type not in tuple(authority.get("supported_claim_types") or ()):
            continue
        if (
            _canonical_authority_claim_strength(authority, state, claim_strength)
            != claim_strength
        ):
            continue
        preserved.append(dict(raw_claim))
    return preserved


def _authority_claims_from_evidence(state: WorkflowState) -> list[dict[str, Any]]:
    """Build verifier inputs from selected authority evidence, never model output."""

    resolution = _required_claim_evidence_resolution(state)
    selected: list[tuple[str, dict[str, Any]]] = []
    if resolution["has_required_claims"]:
        selected.extend(
            (claim_type, dict(entry["evidence"]))
            for claim_type, entry in resolution["required"].items()
            if entry.get("status") == "publishable" and entry.get("evidence")
        )
        selected.extend(
            _publishable_auxiliary_claim_evidence(
                state,
                claim_types=_publication_requested_auxiliary_claim_types(
                    state,
                    resolution["candidate_claim_types"],
                ),
                excluded_claim_types=resolution["required"],
            )
        )
    else:
        primary = dict(resolution.get("primary") or _primary_answer_evidence(state))
        claim_type = str(primary.get("claim_type") or "")
        if primary and claim_type and _evidence_established(primary):
            selected.append((claim_type, primary))

    claims: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for claim_type, evidence in selected:
        evidence_ref = str(evidence.get("evidence_ref") or "")
        signature = (claim_type, evidence_ref)
        if not evidence_ref or signature in seen:
            continue
        base = _default_claim_from_primary_evidence(state, evidence=evidence)
        candidate = {
            **base,
            "evidence_refs": [evidence_ref],
            "numbers": _authority_claim_numbers(evidence, base),
            "scope": _authority_claim_context(evidence, state, "scope"),
            "time_window": _authority_claim_context(evidence, state, "time_window"),
            "claim_type": claim_type,
            "claim_strength": _authority_claim_strength_from_evidence(
                evidence,
                state,
            ),
        }
        dimensions = _authority_claim_dimensions(evidence)
        if dimensions:
            candidate["dimensions"] = dimensions
        normalized = _normalize_authority_claim_candidates([candidate], state)
        if normalized:
            claims.append(normalized[0])
            seen.add(signature)
    return claims


def _publication_requested_auxiliary_claim_types(
    state: WorkflowState,
    candidate_claim_types: Sequence[str],
) -> tuple[str, ...]:
    candidates = {
        str(claim_type)
        for claim_type in candidate_claim_types
        if str(claim_type)
    }
    return tuple(
        claim_type
        for claim_type in (state.get("intent") or {}).get(
            "auxiliary_claim_types", ()
        )
        if claim_type in candidates
    )


def _publishable_auxiliary_claim_evidence(
    state: WorkflowState,
    *,
    claim_types: Sequence[str],
    excluded_claim_types: Mapping[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    evidence_items = tuple(
        (index, item)
        for index, item in enumerate(state.get("evidence") or ())
        if isinstance(item, Mapping)
    )
    selected: list[tuple[str, dict[str, Any]]] = []
    excluded = set(excluded_claim_types)
    for claim_type in claim_types:
        if claim_type in excluded:
            continue
        matching = tuple(
            indexed
            for indexed in evidence_items
            if str(indexed[1].get("claim_type") or "") == claim_type
        )
        if not matching:
            continue
        _, evidence = max(matching, key=_claim_evidence_selection_key)
        if not (
            _evidence_claim_input_ready(evidence)
            and _evidence_established(dict(evidence))
        ):
            continue
        selected.append((claim_type, dict(evidence)))
    return selected


def _authority_claim_numbers(
    evidence: Mapping[str, Any],
    base_claim: Mapping[str, Any],
) -> dict[str, Any]:
    numeric_facts = dict(evidence.get("numeric_facts") or {})
    capability_id = str(
        evidence.get("capability_id") or evidence.get("capability") or ""
    )
    if capability_id == "cross_source_association":
        return {}
    if capability_id == "candidate_dimension_screen":
        return {
            key: value
            for key, value in numeric_facts.items()
            if "paid_amount" in key
        }
    return numeric_facts or dict(base_claim.get("numbers") or {})


def _authority_claim_dimensions(evidence: Mapping[str, Any]) -> dict[str, Any]:
    dimensions = evidence.get("dimensions")
    if isinstance(dimensions, Mapping) and dimensions:
        return dict(dimensions)
    payload = evidence.get("typed_payload") or {}
    if not isinstance(payload, Mapping):
        return {}
    readouts = payload.get("selected_business_readouts") or ()
    if isinstance(readouts, Sequence) and not isinstance(readouts, (str, bytes)):
        selected = {
            str(item.get("dimension") or ""): item.get("value")
            for item in readouts
            if isinstance(item, Mapping)
            and str(item.get("dimension") or "")
            and item.get("value") not in (None, "")
        }
        if selected:
            return selected
    dimension = str(payload.get("selected_dimension") or "").strip()
    value = payload.get("selected_value")
    return {dimension: value} if dimension and value not in (None, "") else {}


def _authority_claim_strength_from_evidence(
    evidence: Mapping[str, Any],
    state: WorkflowState,
) -> str:
    if evidence.get("maximum_claim_strength"):
        strength = _canonical_authority_claim_strength(
            evidence,
            state,
            "strong",
        )
        if strength:
            return strength
    return {
        "high": "high",
        "medium": "medium",
        "directional": "observed",
    }.get(str(evidence.get("strength") or ""), "insufficient")


def _authority_claim_context(
    authority: Mapping[str, Any],
    state: WorkflowState,
    field: str,
) -> str:
    payload = authority.get("typed_payload") or {}
    candidates = (
        authority.get(field),
        payload.get(field) if isinstance(payload, Mapping) else None,
        state.get("intent", {}).get(field),
    )
    return next(
        (
            str(value).strip()
            for value in candidates
            if isinstance(value, str) and value.strip()
        ),
        "",
    )


def _normalize_authority_claim_candidates(
    claims: Any,
    state: WorkflowState,
) -> list[dict[str, Any]]:
    evidence_by_ref = _evidence_by_ref(state.get("evidence", []))
    evidence_refs = set(evidence_by_ref)
    normalized = []
    seen = set()
    claim_candidates = tuple(claims or ())
    for claim in claim_candidates:
        if not isinstance(claim, Mapping):
            continue
        refs = [
            ref for ref in claim.get("evidence_refs", ()) if ref in evidence_refs
        ]
        refs = _prioritize_claim_refs(refs, evidence_by_ref)
        if not refs:
            continue
        bound_authorities = tuple(
            dict(evidence_by_ref.get(ref, {}))
            for ref in refs
            if evidence_by_ref.get(ref, {}).get("binding_manifest_ref")
        )
        authority_candidates = tuple(
            dict(evidence_by_ref.get(ref, {}))
            for ref in refs
            if evidence_by_ref.get(ref, {}).get("binding_manifest_ref")
            and _evidence_claim_input_ready(evidence_by_ref.get(ref, {}))
        )
        if bound_authorities and (
            len(bound_authorities) != 1 or len(authority_candidates) != 1
        ):
            continue
        authority_bound = len(authority_candidates) == 1
        authority = authority_candidates[0] if authority_bound else {}
        supported_claim_types = tuple(authority.get("supported_claim_types") or ())
        claim_type = str(claim.get("claim_type") or "")
        if authority_bound and claim_type not in supported_claim_types:
            claim_type = (
                supported_claim_types[0] if len(supported_claim_types) == 1 else ""
            )
        raw_text = claim.get("text") or claim.get("claim_text") or claim.get("claim")
        has_canonical_contract = bool(
            claim.get("claim_type")
            and (claim.get("claim_strength") or claim.get("strength"))
            and claim.get("scope")
            and claim.get("time_window")
        )
        raw_numbers = claim.get("numbers") or claim.get("numeric_facts")
        if not isinstance(raw_numbers, Mapping):
            authority_numbers = authority.get("numeric_facts") or {}
            direct_numbers = {
                key: claim[key]
                for key in authority_numbers
                if key in claim
            }
            raw_numbers = direct_numbers or (
                authority_numbers
                if authority_bound and (raw_text or has_canonical_contract)
                else {}
            )
        normalized_numbers = _normalize_claim_numbers(
            raw_numbers,
            refs,
            evidence_by_ref,
        )
        text = _weaken_unsupported_causal_wording(
            raw_text
        )
        if not text:
            continue
        dedupe_key = (text, tuple(refs))
        if dedupe_key in seen:
            continue
        scope = (
            _authority_claim_context(authority, state, "scope")
            if authority_bound
            else str(
                claim.get("scope") or state.get("intent", {}).get("scope") or ""
            ).strip()
        )
        time_window = (
            _authority_claim_context(authority, state, "time_window")
            if authority_bound
            else str(
                claim.get("time_window")
                or state.get("intent", {}).get("time_window")
                or ""
            ).strip()
        )
        if not scope or not time_window:
            continue
        seen.add(dedupe_key)
        normalized_claim = {
            "text": str(text),
            "evidence_refs": refs,
            "numbers": normalized_numbers,
            "scope": scope,
            "time_window": time_window,
            "claim_strength": (
                _canonical_authority_claim_strength(
                    authority,
                    state,
                    claim.get("claim_strength") or claim.get("strength"),
                )
                if authority_bound
                else claim.get("claim_strength") or claim.get("strength")
            ),
            "claim_type": claim_type,
        }
        if isinstance(claim.get("dimensions"), Mapping) and claim.get(
            "dimensions"
        ):
            normalized_claim["dimensions"] = {
                str(key): value
                for key, value in claim["dimensions"].items()
            }
        normalized.append(
            _with_claim_audit(
                state,
                normalized_claim,
            )
        )
    return normalized


def _canonical_authority_claim_strength(
    authority: Mapping[str, Any],
    state: WorkflowState,
    requested: Any,
) -> str:
    """Select a claim-taxonomy value within the resolved binding ceiling."""

    registry = state.get("request", {}).get("runtime_registry")
    if not isinstance(registry, RuntimeContractRegistry):
        registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    try:
        ceiling_rank = registry.maximum_claim_strength_rank(
            str(authority.get("maximum_claim_strength") or "")
        )
    except (KeyError, TypeError, ValueError):
        return ""
    candidate = str(requested or "")
    try:
        if registry.claim_strength_rank(candidate) <= ceiling_rank:
            return candidate
    except (KeyError, TypeError, ValueError):
        pass
    ranked = registry.claim_strength_taxonomy.get("claim_strength_ranks") or {}
    allowed = [
        (int(rank), str(name))
        for name, rank in ranked.items()
        if int(rank) <= ceiling_rank
    ]
    return max(allowed, default=(-1, ""))[1]


def _with_claim_audit(state: WorkflowState, claim: dict[str, Any]) -> dict[str, Any]:
    request = state.get("request", {})
    manifest = request.get("context_manifest") or {}
    return {
        **claim,
        "context_manifest_ref": str(manifest.get("manifest_id") or ""),
        "reuse_decisions": list(request.get("reuse_decisions") or []),
    }


def _prioritize_claim_refs(
    refs: Sequence[str],
    evidence_by_ref: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    established = [
        ref for ref in refs if _evidence_established(dict(evidence_by_ref.get(ref, {})))
    ]
    remaining = [ref for ref in refs if ref not in established]
    return [*established, *remaining]


def _normalize_claim_numbers(
    numbers: Any,
    refs: Sequence[str],
    evidence_by_ref: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(numbers, Mapping):
        return {}
    normalized = {}
    available = set()
    for ref in refs:
        available.update((evidence_by_ref.get(ref, {}).get("numeric_facts") or {}).keys())
    for raw_key, raw_value in numbers.items():
        key = str(raw_key)
        if key in available:
            normalized[key] = raw_value
    return normalized


def _answer_synthesis_context(state: WorkflowState) -> dict[str, Any]:
    claim = _default_claim_from_evidence(state)
    primary = _primary_answer_evidence(state)
    payload = primary.get("typed_payload", {})
    return {
        "question_understanding": _question_understanding_sentence(state),
        "analysis_path": _analysis_path_sentence(state),
        "key_findings": {
            "claim_text": claim["text"],
            "pattern_family": state["intent"]["pattern_family"],
            "pattern_label": _pattern_label(state["intent"]["pattern_family"]),
            "metric_label": _business_metric_label(state),
            "primary_capability": primary.get("capability_id") or primary.get("capability"),
            "median_uplift": payload.get("median_uplift"),
            "direction_ratio": payload.get("direction_ratio"),
            "direction_consistency_ratio": payload.get("direction_consistency_ratio"),
            "materiality_hit_ratio": payload.get("materiality_hit_ratio"),
            "comparable_periods": payload.get("comparable_periods"),
            "min_periods": payload.get("min_periods"),
            "materiality_floor": payload.get("materiality_floor"),
            "strength": primary.get("strength"),
            "wording_limit": primary.get("wording_limit"),
            "established": primary.get("established"),
            "numeric_facts": dict(primary.get("numeric_facts") or {}),
            "typed_payload": payload,
        },
        "evidence_boundary": _attention_sentence(state),
        "capability_business_findings": _capability_business_findings(state),
        "verified_claim_slots": _verified_claim_slots(state),
        "bounded_insight_guidance": _bounded_insight_guidance(state),
        "causal_evidence_dossier": state.get("causal_evidence_dossier", {}),
        "causal_audit": state.get("causal_audit", {}),
        "required_answer_shape": [
            "我对问题的理解",
            "分析思路",
            "关键发现",
            "结论",
            "需要注意",
        ],
    }


def _verified_claim_slots(state: WorkflowState) -> list[dict[str, Any]]:
    slots = []
    for index, claim in enumerate(_verified_claims(state)):
        slots.append(
            {
                "claim_ref": f"verified_claim:{index}",
                "business_claim": str(claim.get("text") or ""),
                "numbers": dict(claim.get("numbers") or {}),
                "scope": str(claim.get("scope") or state.get("intent", {}).get("scope") or ""),
                "time_window": str(
                    claim.get("time_window") or state.get("intent", {}).get("time_window") or ""
                ),
                "claim_strength": str(claim.get("claim_strength") or claim.get("strength") or ""),
                "evidence_refs": list(claim.get("evidence_refs") or ()),
                "preservation_guidance": "可以业务化转述，但要保留数字、方向、范围和证据边界。",
            }
        )
    return slots


def _bounded_insight_guidance(state: WorkflowState) -> dict[str, Any]:
    limitations = tuple(state.get("evidence_brief", {}).get("limitations") or ())
    limits = list(_business_limitation_reasons(limitations))
    accepted = (
        tuple(state.get("compiled_graph").mutations.accepted_graph)
        if state.get("compiled_graph")
        else tuple(
            str(item.get("capability_id") or item.get("capability") or "")
            for item in state.get("evidence", ())
            if isinstance(item, Mapping)
        )
    )
    focus = _capability_path_labels(accepted)
    return {
        "insight_prompt": (
            f"给出有边界的业务洞察：当前证据能把排查方向收敛到{focus}；"
            "可以说明已能观察到什么、能排除什么、下一步最小补充什么，不能写成唯一原因。"
        ),
        "evidence_limits": limits,
    }


def _final_summary_needs_display_repair(text: Any, state: WorkflowState) -> bool:
    return bool(_final_summary_display_repair_reasons(text, state))


def _final_summary_display_repair_reasons(text: Any, state: WorkflowState) -> list[str]:
    value = str(text or "")
    reasons = []
    if _has_internal_visible_token(value):
        reasons.append("internal_visible_token")
    claims = state.get("draft_claims") or []
    if not claims and _pattern_evidence(state):
        if not _final_summary_covers_pattern_evidence(value, state):
            reasons.append("missing_pattern_evidence")
        return reasons
    for claim in claims:
        if _is_driver_claim(claim) and not _final_summary_covers_claim(value, claim, state):
            reasons.append("missing_driver_claim")
    if claims and not _final_summary_covers_claim(value, claims[0], state):
        reasons.append("missing_primary_claim")
    return list(dict.fromkeys(reasons))


def _final_summary_covers_claim(
    text: str,
    claim: dict[str, Any],
    state: WorkflowState,
) -> bool:
    numbers = claim.get("numbers", {})
    required = []
    if _is_joint_claim(claim):
        joint = _joint_attribution_evidence(state)
        first = ((joint.get("typed_payload", {}).get("top_combinations") or [{}])[0])
        values = first.get("dimension_values") or ()
        required.extend(_businessize_dimension_value(value) for value in values[:2])
        if "top_3_absolute_delta_share" in numbers:
            required.append(_format_percent(numbers.get("top_3_absolute_delta_share")))
        return _text_covers_required_values(text, required)
    if "median_uplift" in numbers:
        required.append(_format_percent(abs(numbers.get("median_uplift") or 0)))
    if "unit_value_share" in numbers:
        required.append(_format_percent(numbers.get("unit_value_share")))
    if "volume_share" in numbers:
        required.append(_format_percent(numbers.get("volume_share")))
    for key in (
        "paid_users_contribution_share",
        "paid_frequency_contribution_share",
        "avg_order_amount_contribution_share",
    ):
        if key in numbers:
            required.append(_format_percent(numbers.get(key)))
    if state.get("intent", {}).get("pattern_family") == "custom_baseline":
        return _text_covers_required_values(text, required)
    if "direction_ratio" in numbers:
        required.append(_format_percent(numbers.get("direction_ratio")))
    if "comparable_periods" in numbers:
        required.append(str(numbers.get("comparable_periods")))
    return _text_covers_required_values(text, required)


def _final_summary_covers_pattern_evidence(text: str, state: WorkflowState) -> bool:
    pattern = _pattern_evidence(state)
    payload = pattern.get("typed_payload", {})
    required = []
    if "median_uplift" in payload:
        required.append(_format_percent(abs(payload.get("median_uplift") or 0)))
    if state.get("intent", {}).get("pattern_family") == "custom_baseline":
        return _text_covers_required_values(text, required)
    if "direction_ratio" in payload:
        required.append(_format_percent(payload.get("direction_ratio")))
    if "comparable_periods" in payload:
        required.append(str(payload.get("comparable_periods")))
    return _text_covers_required_values(text, required)


def _text_covers_required_values(text: str, required: list[str]) -> bool:
    for item in required:
        if not item:
            continue
        compact_percent = item.replace(".0%", "%")
        if item not in text and compact_percent not in text:
            return False
    return True


def _is_driver_claim(claim: Mapping[str, Any]) -> bool:
    numbers = set((claim.get("numbers") or {}).keys())
    refs = " ".join(str(ref) for ref in claim.get("evidence_refs", ()))
    return "driver_decomposition" in refs or {"unit_value_share", "volume_share"} <= numbers


def _is_joint_claim(claim: Mapping[str, Any]) -> bool:
    refs = " ".join(str(ref) for ref in claim.get("evidence_refs", ()))
    return "joint_attribution" in refs


def _preferred_final_claim(claims: Sequence[dict[str, Any]]) -> dict[str, Any]:
    for claim in claims:
        if _is_driver_claim(claim):
            return claim
    for claim in claims:
        if _is_joint_claim(claim):
            return claim
    return claims[0]


def _question_understanding_sentence(state: WorkflowState) -> str:
    intent = state["intent"]
    metric = _business_metric_label(state)
    scope = _scope_label(intent.get("scope"))
    time_window = intent.get("time_window", "")
    question = str(state.get("request", {}).get("question") or "").strip()
    target_label = _target_label(state)
    baseline_label = _baseline_label(state)
    if intent.get("pattern_family") == "custom_baseline" and target_label and baseline_label:
        base = (
            f"我对问题的理解是：你想看 {target_label} 相比 {baseline_label} 的{metric}"
            f"是否有明显变化，口径是{scope}，观察窗口是 {time_window}。"
        )
    else:
        base = (
            f"我对问题的理解是：你想判断{_pattern_label(intent.get('pattern_family', ''))}"
            f"在 {time_window} 是否成立，指标是{scope}的{metric}。"
        )
    if question:
        return f"{base} 原始问题是：{question}"
    return base


def _analysis_path_sentence(state: WorkflowState) -> str:
    intent = state["intent"]
    target_label = _target_label(state)
    baseline_label = _baseline_label(state)
    compiled = state.get("compiled_graph")
    accepted_graph = tuple(compiled.mutations.accepted_graph) if compiled else ()
    capability_labels = _capability_path_labels(accepted_graph)
    if intent.get("pattern_family") == "custom_baseline":
        comparison = (
            f"把 {baseline_label} 作为基线、{target_label} 作为目标窗口"
            if target_label and baseline_label
            else "把已绑定的基准窗口和目标窗口"
        )
        evaluation_scope = "证据强度和限制项"
    else:
        comparison = f"按{_pattern_label(intent.get('pattern_family', ''))}的业务口径"
        evaluation_scope = "证据强度、可比周期和限制项"
    return (
        f"分析思路：我先确认数据口径和覆盖范围，再{comparison}做聚合对比，"
        f"随后使用{capability_labels}评估{evaluation_scope}。"
    )


def _key_findings_sentence(state: WorkflowState) -> str:
    intent = state["intent"]
    pattern = _pattern_evidence(state)
    if not pattern:
        primary = _primary_business_evidence(state)
        capability = primary.get("capability_id") or primary.get("capability")
        if capability == "driver_decomposition":
            decomp = (primary.get("typed_payload", {}).get("decompositions") or [{}])[0]
            core_driver = str(decomp.get("primary_core_driver") or "")
            if core_driver:
                driver = _driver_core_label(core_driver)
            else:
                driver = (
                    _driver_volume_label(decomp)
                    if decomp.get("primary_driver") == "volume"
                    else _driver_unit_value_label(decomp)
                )
            return f"关键发现：驱动拆解显示，当前变化的主要贡献项是{driver}。"
        if capability == "segment_contribution":
            top_drag = (primary.get("typed_payload", {}).get("top_drags") or [{}])[0]
            return f"关键发现：渠道或分群贡献里，拖累最大的分组是{top_drag.get('segment', '未识别分组')}。"
        if capability == "user_mix_contribution":
            bucket_count = primary.get("typed_payload", {}).get("mix_bucket_count")
            return f"关键发现：用户结构贡献已按聚合口径拆到 {bucket_count} 个用户分层。"
        if capability == "high_value_user_contribution":
            policy = primary.get("typed_payload", {}).get("threshold_policy") or {}
            return f"关键发现：高价值用户贡献已按阈值策略 {policy} 做聚合复核。"
        if capability == "outlier_contribution":
            share = primary.get("typed_payload", {}).get("top_positive_share")
            return f"关键发现：异常贡献检查显示，前几个高贡献周期占正向变化的{_format_percent(share)}。"
        if capability == "joint_attribution":
            return f"关键发现：{_joint_attribution_finding(primary)}"
        return "关键发现：当前证据可以支持一个有边界的业务判断。"
    payload = pattern.get("typed_payload", {})
    median_uplift = payload.get("median_uplift")
    direction_ratio = payload.get("direction_ratio")
    direction_consistency_ratio = payload.get("direction_consistency_ratio", direction_ratio)
    materiality_hit_ratio = payload.get("materiality_hit_ratio", direction_ratio)
    comparable_periods = payload.get("comparable_periods")
    min_periods = payload.get("min_periods")
    materiality_floor = payload.get("materiality_floor")
    target_label = str(intent.get("target", {}).get("label") or "")
    baseline_label = str(intent.get("baseline", {}).get("label") or "")
    metric = _business_metric_label(state)
    if intent.get("pattern_family") == "custom_baseline":
        subject = (
            f"{target_label} 相比 {baseline_label}"
            if target_label and baseline_label
            else "目标窗口相比基准窗口"
        )
        change = f"{metric}{_change_word(median_uplift)} {_format_percent(abs(median_uplift or 0))}"
        if pattern.get("limitations") or pattern.get("wording_limit") == "insufficient":
            limitation = _custom_baseline_limitation_phrase(pattern)
            return f"关键发现：{subject}，观察到{change}；但{limitation}。"
        return (
            f"关键发现：{subject}，核心结果是{change}，"
            f"超过当前重要性阈值 {_format_percent(materiality_floor)}。"
        )
    else:
        subject = _pattern_label(intent.get("pattern_family", ""))
        change = _median_change_phrase(median_uplift)
    return (
        f"关键发现：{subject}，核心结果是{change}，方向一致比例 "
        f"{_format_percent(direction_consistency_ratio)}，达到重要性阈值的比例 "
        f"{_format_percent(materiality_hit_ratio)}，当前有 {comparable_periods} 个可比周期；"
        f"本轮要求的最低可比周期是 {min_periods}，"
        f"当前重要性阈值是 {_format_percent(materiality_floor)}。"
    )


def _conclusion_sentence(state: WorkflowState, claim: dict[str, Any]) -> str:
    claim_text = _normalize_visible_business_text(str(claim["text"]), state)
    pattern = _pattern_evidence(state)
    payload = pattern.get("typed_payload", {})
    median_uplift = _as_float(payload.get("median_uplift"))
    materiality_floor = _as_float(payload.get("materiality_floor"))
    if median_uplift is None or materiality_floor is None:
        return f"结论：{claim_text}"
    if _pattern_has_negative_answer_evidence(state):
        target_claim = str(state.get("intent", {}).get("target_claim") or "这个目标模式")
        return f"结论：当前证据不支持“{target_claim}”。{claim_text}"
    if abs(median_uplift) >= materiality_floor:
        return f"结论：按当前重要性阈值，这个窗口内有可观察的明显变化。{claim_text}"
    return f"结论：按当前重要性阈值，目前不足以支持明显变化。{claim_text}"


def _median_change_phrase(value: Any) -> str:
    numeric = _as_float(value)
    if numeric is None:
        return "中位变化未知"
    if numeric > 0:
        return f"中位提升 {_format_percent(numeric)}"
    if numeric < 0:
        return f"中位下降 {_format_percent(abs(numeric))}"
    return "中位变化 0.0%"


def _attention_sentence(state: WorkflowState) -> str:
    intent = state.get("intent", {})
    pattern = _pattern_evidence(state)
    payload = pattern.get("typed_payload", {})
    comparable_periods = payload.get("comparable_periods")
    limitations = tuple(state.get("evidence_brief", {}).get("limitations", ()))
    notes = []
    if _as_int(comparable_periods) <= 1:
        if intent.get("pattern_family") == "custom_baseline":
            notes.append("这是一组目标期对基准期的窗口对比，结论只适用于当前窗口，不能外推为长期稳定规律")
        else:
            notes.append("当前只有 1 个可比周期，适合做窗口对比，不能外推为长期稳定规律")
    if "insufficient_comparable_periods" in limitations:
        notes.append("可比周期数不足，后续需要补充更多周期验证稳定性")
    if "weak_direction" in limitations:
        notes.append("方向一致性不足，结论要保留波动可能")
    if "below_materiality_floor" in limitations:
        notes.append("变化幅度未达到当前重要性阈值")
    if "no_event_contract_or_matches" in limitations:
        notes.append("暂时没有事件或机制证据，不能解释变化由什么原因造成")
    if "no_comparable_periods" in limitations:
        notes.append("当前没有可比周期，无法形成观察性对比结论")
    if "skipped_incomplete_joint_combinations" in limitations:
        notes.append("部分渠道和阶段组合缺少完整的目标期或基准期配对，已从组合贡献计算中跳过")
    if "sparse_cell" in limitations:
        notes.append("样本过少的组合已跳过，避免让小样本放大结论")
    if any(str(item).startswith("missing_required_field:channel") for item in limitations):
        notes.append("少量聚合行缺少渠道字段，组合归因只使用字段完整的组合")
    if not notes:
        notes.append("后续可以继续跟踪新周期、异常日、渠道或用户结构，确认这个变化是否延续")
    return "需要注意：" + "；".join(dict.fromkeys(notes)) + "。"


def _custom_baseline_limitation_phrase(pattern: dict[str, Any]) -> str:
    payload = pattern.get("typed_payload", {})
    limitations = tuple(pattern.get("limitations", ()))
    parts = []
    comparable = payload.get("comparable_periods")
    minimum = payload.get("min_periods")
    if "insufficient_comparable_periods" in limitations:
        parts.append(f"有效可比周期为 {comparable} 个，低于本轮要求的 {minimum} 个")
    if "weak_direction" in limitations:
        parts.append("方向一致性不足")
    if "below_materiality_floor" in limitations:
        parts.append("典型变化幅度没有达到当前重要性阈值")
    exceptions = payload.get("exceptions") or []
    outlier_count = sum(1 for item in exceptions if item.get("reason") == "outlier_dominated")
    failed_count = sum(1 for item in exceptions if item.get("reason") == "failed_direction")
    if outlier_count:
        parts.append(f"{outlier_count} 个周期因异常占比过高被排除")
    if failed_count:
        parts.append(f"{failed_count} 个周期方向相反或未达阈值")
    if not parts:
        parts.append("当前证据强度不足，不能支撑稳定结论")
    return "，".join(dict.fromkeys(parts))


def _driver_volume_label(decomp: Mapping[str, Any]) -> str:
    key = str(decomp.get("volume_key") or "")
    if key == "paid_users":
        return "付费用户数"
    if key == "orders":
        return "订单数"
    return "数量规模"


def _driver_unit_value_label(decomp: Mapping[str, Any]) -> str:
    key = str(decomp.get("volume_key") or "")
    if key == "paid_users":
        return "单付费用户金额"
    if key == "orders":
        return "单均订单金额"
    return "单位价值"


def _driver_core_label(component_id: Any) -> str:
    return {
        "paid_users": "付费人数",
        "paid_frequency": "付费频次",
        "avg_order_amount": "单笔付费金额",
    }.get(str(component_id or ""), "核心组成因素")


def _capability_path_labels(accepted_graph: tuple[str, ...]) -> str:
    selected = _capability_labels(accepted_graph)
    if not selected:
        return "已接受分析路径"
    return "、".join(dict.fromkeys(selected))


def _business_threads(state: WorkflowState) -> list[dict[str, str]]:
    intent = state.get("intent", {})
    families = list(intent.get("question_families") or ())
    if not families and intent.get("question_family"):
        families = [intent["question_family"]]
    primary = intent.get("primary_question_family") or intent.get("question_family") or ""
    return [
        {
            "question_family": str(family),
            "label": _question_family_label(str(family)),
            "role": "primary" if family == primary else "secondary",
        }
        for family in families
        if family
    ]


def _question_family_label(family: str) -> str:
    labels = {
        "pattern_explanation": "模式解释",
        "paid_amount_change_explanation": "付费金额变化解释",
        "business_object_impact_review": "业务对象影响评估",
        "segment_or_factor_attribution": "分群或因素归因",
        "revenue_health_review": "收入健康评估",
        "anomaly_or_black_swan_review": "异常或突发因素评估",
        "custom_baseline_comparison": "自定义基线对比",
        "data_quality_or_evidence_review": "数据质量或证据评估",
    }
    return labels.get(family, family)


def _capability_labels(accepted_graph: tuple[str, ...]) -> list[str]:
    labels = {
        "data_quality_check": "数据质量检查",
        "data_quality_profile": "数据质量检查",
        "pattern_scan": "对比模式检验",
        "compare_periods": "周期对比",
        "compare_period_phases": "周期对比",
        "answer_verify": "答案校验",
        "formula_decompose": "指标口径拆解",
        "driver_decomposition": "驱动拆解",
        "event_evidence": "事件或机制证据检查",
        "cross_source_association": "玩法与付费时序关联",
        "cross_source_panel_association": "玩法与付费渠道稳健性检验",
        "segment_bridge": "分群结构检查",
        "segment_contribution": "渠道或分群贡献",
        "candidate_dimension_screen": "渠道、支付方式、地区与设备维度定位",
        "user_mix_contribution": "新老用户结构贡献",
        "high_value_user_contribution": "高价值用户贡献",
        "outlier_scan": "异常波动检查",
        "outlier_contribution": "异常贡献检查",
        "joint_attribution": "组合归因",
    }
    return [labels.get(item, item) for item in accepted_graph if item in labels]


def _pattern_evidence(state: WorkflowState) -> dict[str, Any]:
    pattern_family = state["intent"]["pattern_family"]
    for item in state.get("evidence", []):
        if item.get("capability_id") not in PATTERN_COMPARE_CAPABILITIES:
            continue
        if item.get("typed_payload", {}).get("pattern_family") == pattern_family:
            return item
    by_ref = _evidence_by_ref(state.get("evidence", []))
    legacy = by_ref.get(f"pattern_scan:{pattern_family}")
    if legacy:
        return legacy
    return {}


def _primary_business_evidence(state: WorkflowState) -> dict[str, Any]:
    priority = (
        "driver_decomposition",
        "segment_contribution",
        "user_mix_contribution",
        "high_value_user_contribution",
        "outlier_contribution",
        "joint_attribution",
        "formula_decompose",
        "segment_bridge",
        "outlier_scan",
        "data_quality_profile",
        "data_quality_check",
    )
    by_capability = {
        item.get("capability_id") or item.get("capability"): item
        for item in state.get("evidence", [])
    }
    answer_capabilities = {
        capability
        for capability in priority
        if capability not in {"data_quality_profile", "data_quality_check"}
    }
    for capability in priority:
        if capability not in answer_capabilities:
            continue
        item = by_capability.get(capability)
        if item and _evidence_established(item):
            return item
    for capability in priority:
        if capability in by_capability:
            return by_capability[capability]
    return (state.get("evidence") or [{}])[0]


def _joint_attribution_evidence(state: WorkflowState) -> dict[str, Any]:
    for item in state.get("evidence", []):
        if (item.get("capability_id") or item.get("capability")) == "joint_attribution":
            return item
    return {}


def _evidence_established(evidence: dict[str, Any]) -> bool:
    if (
        evidence.get("binding_manifest_ref")
        and not _evidence_claim_input_ready(evidence)
    ):
        return False
    if "established" in evidence:
        return bool(evidence.get("established"))
    if (
        evidence.get("strength") == "directional"
        and evidence.get("wording_limit") == "quantified"
        and _evidence_claim_input_ready(evidence)
        and bool(evidence.get("binding_manifest_ref"))
        and not tuple(evidence.get("limitations") or ())
        and evidence.get("evidence_type")
        in tuple(evidence.get("supported_evidence_types") or ())
        and evidence.get("claim_type")
        in tuple(evidence.get("supported_claim_types") or ())
        and evidence.get("maximum_claim_strength") == "directional"
    ):
        return True
    return evidence.get("strength") in {"high", "medium"} and evidence.get(
        "wording_limit"
    ) in {
        "supported",
        "quantified",
        "stable_association",
        "contextual",
        "candidate",
    }


def _evidence_claim_input_ready(evidence: Mapping[str, Any]) -> bool:
    return evidence.get("claim_input_ready") is True


def _sanitize_terminal_explanation(
    explanation: dict[str, Any],
    state: WorkflowState,
    status: str,
) -> dict[str, Any]:
    if not isinstance(explanation, Mapping):
        raise WorkflowFailure(
            f"{status}_explanation_rejected:object_invalid",
            failure_type="llm",
        )
    value = dict(explanation or {})
    value.pop("owner", None)
    value["status"] = status
    for key in ("explanation", "repair_path"):
        raw = value.get(key)
        if isinstance(raw, str) and not raw.strip():
            raise WorkflowFailure(
                f"{status}_explanation_rejected:{key}_missing",
                failure_type="llm",
            )
        if not isinstance(raw, str) or raw != raw.strip():
            raise WorkflowFailure(
                f"{status}_explanation_rejected:{key}_invalid",
                failure_type="llm",
            )
    visible_text = " ".join(
        value[key] for key in ("explanation", "repair_path")
    )
    if status == "blocked" and _blocked_terminal_text_contradicts_status(visible_text):
        raise WorkflowFailure(f"{status}_explanation_rejected:contradicts_status", failure_type="llm")
    if _has_internal_visible_token(visible_text):
        raise WorkflowFailure(f"{status}_explanation_rejected:internal_tokens", failure_type="llm")
    if _has_materiality_data_volume_drift(visible_text, state):
        raise WorkflowFailure(f"{status}_explanation_rejected:materiality_drift", failure_type="llm")
    if _has_unsupported_comparable_period_drift(visible_text, state):
        raise WorkflowFailure(f"{status}_explanation_rejected:unsupported_period_drift", failure_type="llm")
    if _has_degraded_data_collection_or_contract_drift(visible_text, state):
        raise WorkflowFailure(f"{status}_explanation_rejected:data_or_contract_drift", failure_type="llm")
    if _terminal_target_metric_drift(
        str(value.get("explanation") or ""),
        state,
    ):
        raise WorkflowFailure(
            f"{status}_explanation_rejected:target_metric_drift",
            failure_type="llm",
        )
    repair_path = value["repair_path"]
    if _has_internal_visible_token(repair_path):
        raise WorkflowFailure(
            f"{status}_explanation_rejected:repair_path_internal_tokens",
            failure_type="llm",
        )
    if _repair_path_invents_fixed_future_window(repair_path):
        raise WorkflowFailure(
            f"{status}_explanation_rejected:repair_path_future_window",
            failure_type="llm",
        )
    gap_ids = [
        str(item.get("gap_id"))
        for item in state.get("contract_gap_diagnostics") or ()
        if isinstance(item, Mapping) and item.get("gap_id")
    ]
    accepted_choice = _authority_accepted_degradation_choice(state)
    next_action_ids = [
        str(identifier)
        for identifier in (
            accepted_choice.get("choice_id"),
            accepted_choice.get("action_kind"),
        )
        if identifier
    ]
    value.update({
        "boundary_only": True,
        "used_contract_gap_ids": gap_ids,
        "used_next_action_ids": next_action_ids,
        "structured_claim_ids": [],
    })
    return value


def _terminal_target_metric_drift(text: str, state: WorkflowState) -> bool:
    target = str((state.get("intent") or {}).get("target_metric") or "")
    registry = state.get("request", {}).get("runtime_registry")
    if not isinstance(registry, RuntimeContractRegistry):
        registry = RuntimeContractRegistry.from_path(
            CANONICAL_RUNTIME_BINDINGS_PATH
        )
    try:
        target_labels = registry.metric_business_labels(target)
    except (KeyError, TypeError, ValueError):
        return False
    if not target_labels or any(label in text for label in target_labels):
        return False
    return any(
        label in text
        for metric in registry.metric_ids
        if metric != target
        for label in registry.metric_business_labels(metric)
    )


def _blocked_terminal_text_contradicts_status(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "无需阻塞",
            "无需修复",
            "不需要阻塞",
            "不用阻塞",
            "所有检查已通过",
            "验证全部通过",
        )
    )


def _has_materiality_data_volume_drift(text: str, state: WorkflowState) -> bool:
    limitations = tuple(state.get("evidence_brief", {}).get("limitations", ()))
    if "below_materiality_floor" not in limitations:
        return False
    return any(
        phrase in text
        for phrase in (
            "数据量低于",
            "样本量",
            "调整重要性阈值以确认",
            "提升数据敏感性",
            "数据源完整性",
            "提取逻辑",
            "数据质量不足",
            "调整重要性阈值",
            "重新评估付费金额的重要性阈值",
            "复核重要性阈值",
        )
    )


def _has_materiality_ratio_threshold_drift(text: str) -> bool:
    sentences = re.split(r"[。；;.!?？\n]+", text)
    for sentence in sentences:
        if not sentence:
            continue
        if "重要性阈值" not in sentence:
            continue
        if "达到重要性阈值的比例" not in sentence and "方向一致比例" not in sentence:
            continue
        if any(marker in sentence for marker in ("低于", "高于", "超过", "小于", "大于")):
            return True
    return False


def _has_unsupported_comparable_period_drift(text: str, state: WorkflowState) -> bool:
    limitations = tuple(state.get("evidence_brief", {}).get("limitations", ()))
    if any(
        item in limitations
        for item in ("insufficient_comparable_periods", "no_comparable_periods")
    ):
        return False
    return any(
        phrase in text
        for phrase in (
            "可比较期间数量不足",
            "可比周期数不足",
            "可比较月份不足",
            "可比较期间不足",
        )
    )


def _repair_path_invents_fixed_future_window(text: str) -> bool:
    return bool(re.search(r"\d+\s*个月(?:以上|后|内)", text))


def _has_degraded_data_collection_or_contract_drift(text: str, state: WorkflowState) -> bool:
    limitations = tuple(state.get("evidence_brief", {}).get("limitations", ()))
    data_gap = any(
        item in limitations
        for item in (
            "no_rows",
            "insufficient_values",
            "insufficient_comparable_periods",
            "no_comparable_periods",
        )
    )
    if not data_gap and any(token in text for token in ("收集更多数据", "积累更多月度数据")):
        return True
    if "合同依据" in text:
        return True
    return False


def _business_limitation_reasons(limitations: tuple[str, ...]) -> tuple[str, ...]:
    mapping = {
        "insufficient_comparable_periods": "可比周期数不足",
        "weak_direction": "方向一致性不足",
        "below_materiality_floor": "变化幅度低于当前重要性阈值",
        "no_event_contract_or_matches": "缺少可支持机制解释的事件证据",
        "no_comparable_periods": "没有可比周期",
        "insufficient_values": "可用于异常判断的数据点不足",
        "no_rows": "当前查询没有返回可分析数据",
    }
    reasons = [mapping.get(item) for item in limitations if mapping.get(item)]
    return tuple(dict.fromkeys(reasons))


def _business_validator_reasons(validator_results: Any) -> tuple[str, ...]:
    reasons = []
    for item in validator_results or ():
        if item.get("ok", True):
            continue
        reason = str(item.get("reason") or "校验未通过")
        reasons.append(_strip_internal_tokens(reason))
    return tuple(dict.fromkeys(reasons))


def _terminal_repair_path(state: WorkflowState, status: str) -> str:
    if state.get("clarification_outcome", {}).get("boundary_status") == "needs_question":
        return "确认澄清选项，或接受推荐业务假设后继续。"
    if status == "blocked":
        return "先修复固定敏感输出、源数据访问、合同、数据覆盖或问题边界后重跑。"
    limitations = tuple(state.get("evidence_brief", {}).get("limitations", ()))
    actions = []
    if "insufficient_comparable_periods" in limitations or "no_comparable_periods" in limitations:
        actions.append("补充更多可比周期")
    if "below_materiality_floor" in limitations or "weak_direction" in limitations:
        actions.append("继续观察新周期并复核方向一致性")
    if "no_event_contract_or_matches" in limitations:
        actions.append("补充事件或机制证据")
    if "insufficient_values" in limitations or "no_rows" in limitations:
        actions.append("检查数据覆盖和聚合口径")
    if not actions:
        actions.append("补充证据后重跑")
    return "；".join(dict.fromkeys(actions)) + "。"


def _has_internal_visible_token(text: Any) -> bool:
    value = str(text or "")
    tokens = (
        "paid_amount",
        "payment_amount",
        "phase",
        "pattern_status",
        "pattern_established",
        "wording_limit",
        "pattern_scan",
        "data_quality_check",
        "evidence_ref",
        "custom_baseline",
        "intra_period",
        "event_evidence",
        "segment_bridge",
        "outlier_scan",
        "data_engineering_owner",
        "business_analysis_owner",
        "all_users",
        "monthly_daily_avg",
    )
    return any(token in value for token in tokens)


def _businessize_internal_tokens(text: str) -> str:
    value = text
    replacements = {
        "paid_amount": "付费金额",
        "payment_amount": "付费金额",
        "pattern_status": "模式状态",
        "pattern_established": "模式是否成立",
        "wording_limit": "措辞边界",
        "pattern_scan": "模式检验",
        "data_quality_check": "数据质量检查",
        "evidence_ref": "证据引用",
        "custom_baseline": "自定义基线对比",
        "intra_period": "周期内对比",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    return value


def _strip_internal_tokens(text: str) -> str:
    return _businessize_internal_tokens(text)


def _default_claim_from_evidence(state: WorkflowState) -> dict[str, Any]:
    pattern_family = state["intent"]["pattern_family"]
    pattern = _pattern_evidence(state)
    primary = _primary_answer_evidence(state)
    if not pattern or pattern.get("evidence_ref") != primary.get("evidence_ref"):
        return _default_claim_from_primary_evidence(state, evidence=primary)
    payload = pattern.get("typed_payload", {})
    median_uplift = payload.get("median_uplift")
    comparable_periods = payload.get("comparable_periods")
    direction_ratio = payload.get("direction_ratio")
    direction_consistency_ratio = payload.get("direction_consistency_ratio", direction_ratio)
    materiality_hit_ratio = payload.get("materiality_hit_ratio", direction_ratio)
    limitation_text = (
        " 机制证据暂不可用。"
        if "no_event_contract_or_matches" in state.get("evidence_brief", {}).get("limitations", ())
        else ""
    )
    target_label = str(state["intent"].get("target", {}).get("label") or "")
    baseline_label = str(state["intent"].get("baseline", {}).get("label") or "")
    metric = _business_metric_label(state)
    negative_prefix = ""
    if _pattern_has_negative_answer_evidence(state):
        target_claim = str(state.get("intent", {}).get("target_claim") or "目标模式")
        negative_prefix = f"当前证据不支持“{target_claim}”。"
    if pattern_family == "custom_baseline":
        subject = (
            f"{target_label} 相比 {baseline_label}"
            if target_label and baseline_label
            else "目标窗口相比基准窗口"
        )
        text = (
            f"{negative_prefix}"
            f"{subject} 在 {state['intent']['time_window']} 观察到："
            f"{metric}{_change_word(median_uplift)} {_format_percent(abs(median_uplift or 0))}。"
            f"{limitation_text}"
        )
    else:
        text = (
            f"{negative_prefix}"
            f"{_pattern_label(pattern_family)}在 {state['intent']['time_window']} 观察到："
            f"{_median_change_phrase(median_uplift)}，"
            f"方向一致比例 {_format_percent(direction_consistency_ratio)}，"
            f"达到重要性阈值的比例 {_format_percent(materiality_hit_ratio)}，"
            f"{comparable_periods} 个可比周期。{limitation_text}"
        )
    return _with_claim_audit(
        state,
        {
            "text": text,
            "evidence_refs": [
                pattern.get("evidence_ref", f"pattern_scan:{pattern_family}")
            ],
            "numbers": {
                "median_uplift": median_uplift,
                "comparable_periods": comparable_periods,
                "direction_ratio": direction_ratio,
                "direction_consistency_ratio": payload.get(
                    "direction_consistency_ratio", direction_ratio
                ),
                "materiality_hit_ratio": payload.get("materiality_hit_ratio", direction_ratio),
            },
            "scope": state["intent"]["scope"],
            "time_window": state["intent"]["time_window"],
        },
    )


def _default_claim_from_primary_evidence(
    state: WorkflowState,
    *,
    evidence: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    evidence = dict(evidence or _primary_business_evidence(state))
    payload = evidence.get("typed_payload", {})
    capability = evidence.get("capability_id") or evidence.get("capability")
    if capability == "compare_periods":
        target_label = str(state.get("intent", {}).get("target", {}).get("label") or "目标日")
        baseline_label = str(
            state.get("intent", {}).get("baseline", {}).get("label") or "基准日"
        )
        target_value = payload.get("target_value")
        baseline_value = payload.get("baseline_value")
        absolute_change = payload.get("absolute_change")
        relative_change = _as_float(payload.get("relative_change"))
        if relative_change is None:
            direction = "发生变化"
            percent_text = ""
        elif relative_change > 0:
            direction = "上涨"
            percent_text = f" {abs(relative_change) * 100:.2f}%"
        elif relative_change < 0:
            direction = "下跌"
            percent_text = f" {abs(relative_change) * 100:.2f}%"
        else:
            direction = "持平"
            percent_text = ""
        delta_prefix = "增加" if _as_float(absolute_change) and _as_float(absolute_change) > 0 else "减少"
        if _as_float(absolute_change) == 0:
            delta_prefix = "变化"
        text = (
            f"{target_label}的{_business_metric_label(state)}为"
            f"{_format_number(target_value)}，较{baseline_label}的"
            f"{_format_number(baseline_value)}{direction}{percent_text}"
            f"（{delta_prefix}{_format_number(abs(_as_float(absolute_change) or 0))}）。"
        )
        numbers = {
            "target_value": target_value,
            "baseline_value": baseline_value,
            "absolute_change": absolute_change,
            "relative_change": relative_change,
        }
    elif capability == "driver_decomposition":
        decomp = (payload.get("decompositions") or [{}])[0]
        core_contributions = tuple(decomp.get("core_factor_contributions") or ())
        if core_contributions:
            primary_label = _driver_core_label(decomp.get("primary_core_driver"))
            contribution_text = "，".join(
                f"{_driver_core_label(item.get('component_id'))}贡献 "
                f"{_format_percent(item.get('contribution_share'))}"
                for item in core_contributions
            )
            text = (
                f"当前拆解显示，{state['intent']['time_window']} 内付费金额变化的主要贡献项是"
                f"{primary_label}；{contribution_text}。"
                "首充人数作为付费人数的下钻观察，不重复计入三因素贡献。"
            )
            assumption = decomp.get("payment_success_assumption") or {}
            if assumption.get("observed") is False:
                text += (
                    "支付成功率缺少独立观测，本轮按不变处理，"
                    "不参与单独贡献判断。"
                )
            numbers = {
                key: payload.get(key)
                for key in (
                    "paid_users_contribution_share",
                    "paid_frequency_contribution_share",
                    "avg_order_amount_contribution_share",
                    "amount_delta_ratio",
                )
                if payload.get(key) is not None
            }
        else:
            primary = decomp.get("primary_driver")
            volume_label = _driver_volume_label(decomp)
            unit_label = _driver_unit_value_label(decomp)
            primary_label = volume_label if primary == "volume" else unit_label
            text = (
                f"当前拆解显示，{state['intent']['time_window']} 内付费金额变化的主要贡献项是"
                f"{primary_label}；{volume_label}贡献 {_format_percent(decomp.get('volume_share'))}，"
                f"{unit_label}贡献 {_format_percent(decomp.get('unit_value_share'))}。"
            )
            numbers = {
                "volume_share": decomp.get("volume_share"),
                "unit_value_share": decomp.get("unit_value_share"),
                "amount_delta_ratio": decomp.get("amount_delta_ratio"),
            }
    elif capability == "candidate_dimension_screen":
        selected_dimension = str(
            payload.get("selected_dimension_label")
            or payload.get("selected_dimension")
            or "候选维度"
        )
        selected_value = str(payload.get("selected_value") or "待定位分组")
        text = str(
            payload.get("business_readout")
            or (
                f"{selected_dimension}是当前优先排查维度，"
                f"重点关注{selected_value}。该优先级用于定位，"
                "跨维度不可相加。"
            )
        )
        numbers = {
            key: evidence.get("numeric_facts", {}).get(key)
            for key in (
                "paid_amount_target_value",
                "paid_amount_baseline_value",
                "paid_amount_delta",
                "paid_amount_relative_change",
            )
            if evidence.get("numeric_facts", {}).get(key) is not None
        }
    elif capability == "cross_source_association":
        text = cross_source_auxiliary_claim_text(payload)
        numbers = {}
    elif capability == "segment_contribution":
        top_drag = (payload.get("top_drags") or [{}])[0]
        text = (
            f"当前渠道/分群贡献拆解显示，拖累最大的分组是"
            f"{top_drag.get('segment', '未识别分组')}，变化 "
            f"{_format_number(top_drag.get('delta'))}。"
        )
        numbers = {
            "total_delta": payload.get("total_delta"),
            "segment_count": payload.get("segment_count"),
        }
    elif capability == "user_mix_contribution":
        text = (
            f"当前新老用户结构贡献只基于聚合分群口径观察，"
            f"覆盖 {payload.get('mix_bucket_count')} 个用户分层、"
            f"{payload.get('segment_count')} 个分群。"
        )
        numbers = {
            "total_amount": payload.get("total_amount"),
            "total_paid_users": payload.get("total_paid_users"),
            "mix_bucket_count": payload.get("mix_bucket_count"),
            "segment_count": payload.get("segment_count"),
        }
    elif capability == "high_value_user_contribution":
        text = str(
            payload.get("business_readout")
            or (
                f"当前高价值用户贡献按聚合阈值策略 {payload.get('threshold_policy')} 复核，"
                f"覆盖总金额 {_format_number(payload.get('total_amount'))}。"
            )
        )
        numbers = {
            "total_amount": payload.get("total_amount"),
            "total_paid_users": payload.get("total_paid_users"),
            "high_value_amount": payload.get("high_value_amount"),
        }
    elif capability == "outlier_contribution":
        text = (
            f"当前异常贡献检查显示，前几个高贡献周期贡献占正向变化的"
            f"{_format_percent(payload.get('top_positive_share'))}。"
        )
        numbers = {
            "top_positive_share": payload.get("top_positive_share"),
            "total_delta": payload.get("total_delta"),
            "paired_periods": payload.get("paired_periods"),
        }
    elif capability == "joint_attribution":
        text = (
            f"当前组合贡献拆解显示，渠道和月内阶段组合可以作为候选解释："
            f"{_joint_attribution_finding(evidence)}"
            "这仍是观察性归因，不能直接写成原因定论。"
        )
        numbers = {
            "total_delta": payload.get("total_delta"),
            "absolute_total_delta": payload.get("absolute_total_delta"),
            "combination_count": payload.get("combination_count"),
            "skipped_sparse_rows": payload.get("skipped_sparse_rows"),
            "leading_absolute_delta_share": payload.get("leading_absolute_delta_share"),
            "top_3_absolute_delta_share": payload.get("top_3_absolute_delta_share"),
        }
    else:
        text = "当前证据支持一个有边界的业务判断，但需要在答案中保留限制项。"
        numbers = {}
    return _with_claim_audit(
        state,
        {
            "text": text,
            "evidence_refs": [evidence.get("evidence_ref")],
            "numbers": numbers,
            "scope": state["intent"]["scope"],
            "time_window": state["intent"]["time_window"],
        },
    )


def _capability_business_findings(state: WorkflowState) -> list[dict[str, Any]]:
    findings = []
    for item in state.get("evidence", []):
        capability = item.get("capability_id") or item.get("capability")
        if not capability:
            continue
        payload = item.get("typed_payload", {})
        result_refs = [str(ref) for ref in (item.get("result_refs") or []) if ref]
        evidence_ref = item.get("evidence_ref")
        finding = {
            "capability": capability,
            "business_readout": payload.get("business_readout"),
            "claim_boundary": payload.get("claim_boundary"),
            "evidence_refs": result_refs or ([evidence_ref] if evidence_ref else []),
        }
        optional_values = {
            "analysis_role": payload.get("analysis_role"),
            "selected_dimension": payload.get("selected_dimension"),
            "selected_dimension_label": payload.get("selected_dimension_label"),
            "selected_value": payload.get("selected_value"),
            "numeric_facts": dict(item.get("numeric_facts") or {}),
        }
        finding.update(
            {
                key: value
                for key, value in optional_values.items()
                if value not in (None, "", {}, ())
            }
        )
        findings.append(finding)
    return findings


def _format_number(value: Any) -> str:
    numeric = _as_float(value)
    if numeric is None:
        return "未知"
    return f"{numeric:,.1f}"


def _change_word(value: Any) -> str:
    try:
        return "下降" if float(value) < 0 else "提升"
    except (TypeError, ValueError):
        return "变化"


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metric_label(metric: Any) -> str:
    return {
        "paid_amount": "付费金额",
        "payment_amount": "付费金额",
        "monthly_daily_avg_paid_amount": "月度日均付费金额",
        "monthly_avg_paid_amount": "月度日均付费金额",
        "revenue": "收入",
    }.get(str(metric or ""), str(metric or "目标指标"))


def _business_metric_label(state: Mapping[str, Any]) -> str:
    intent = state.get("intent", {})
    metric = _metric_label(intent.get("target_metric"))
    text = " ".join(
        str(value or "")
        for value in (
            intent.get("target_claim"),
            state.get("request", {}).get("question") if isinstance(state.get("request"), dict) else "",
        )
    )
    if metric == "付费金额" and any(marker in text for marker in ("日均", "日平均")):
        return "日均付费金额"
    return metric


def _joint_attribution_finding(evidence: Mapping[str, Any]) -> str:
    payload = evidence.get("typed_payload", {})
    top = list(payload.get("top_combinations") or ())
    if not top:
        return "组合归因没有形成可比较的渠道和阶段组合。"
    first = top[0]
    first_label = _joint_combination_label(first)
    first_share = _format_percent(first.get("absolute_delta_share"))
    first_delta = _format_number(first.get("delta"))
    top_three = [_joint_combination_label(item) for item in top[:3]]
    top_three_share = _format_percent(payload.get("top_3_absolute_delta_share"))
    if len(top_three) >= 3:
        return (
            f"贡献最大的组合是{first_label}，增量 {first_delta}，占绝对变化 {first_share}；"
            f"前三个组合是{'、'.join(top_three)}，合计占绝对变化 {top_three_share}。"
        )
    return (
        f"贡献最大的组合是{first_label}，增量 {first_delta}，占绝对变化 {first_share}。"
    )


def _joint_combination_label(item: Mapping[str, Any]) -> str:
    values = item.get("dimension_values") or ()
    labels = [_businessize_dimension_value(value) for value in values]
    return " × ".join(labels) if labels else "未识别组合"


def _businessize_dimension_value(value: Any) -> str:
    text = str(value)
    return {
        "start": "月初",
        "mid": "月中",
        "end": "月末",
        "baseline": "基准期",
        "target": "目标期",
    }.get(text, text)


def _scope_label(scope: Any) -> str:
    return {
        "full_sample": "全样本",
        "all_users": "全体用户",
    }.get(str(scope or ""), str(scope or "当前范围"))


def _pattern_label(pattern_family: str) -> str:
    return {
        "weekly": "周维度付费金额模式",
        "rolling": "滚动窗口付费金额模式",
        "custom_baseline": "自定义基线付费金额对比",
        "intra_period": "周期内付费金额模式",
        "event_relative": "事件相对窗口付费金额模式",
        "lag_recovery": "滞后/恢复付费金额模式",
    }.get(pattern_family, f"{pattern_family} 付费金额模式")


def _weaken_unsupported_causal_wording(text: Any) -> str:
    value = str(text or "")
    value = value.replace("主要驱动是", "主要贡献项是")
    value = value.replace("主要驱动因素为", "主要贡献项是")
    value = value.replace("主要驱动因素是", "主要贡献项是")
    value = value.replace("主要驱动力为", "主要贡献项是")
    value = value.replace("主要驱动力是", "主要贡献项是")
    value = value.replace("驱动因素", "贡献项")
    value = value.replace("驱动力", "贡献项")
    value = value.replace("不能直接写成因果结论", "不能直接写成原因定论")
    value = value.replace("不能直接定因果", "不能直接定为原因")
    value = value.replace("因果证据", "机制证据")
    value = value.replace("因果结论", "原因定论")
    value = value.replace("因果", "原因")
    value = re.sub(r"\b(caused|causes|causing|cause)\b", "is associated with", value, flags=re.I)
    value = re.sub(r"\bdue to\b", "with", value, flags=re.I)
    value = value.replace(
        "No event-based causes were identified to explain the pattern.",
        "没有匹配到可支持机制结论的事件证据。",
    )
    value = value.replace(
        "No causal events were matched, so the pattern's underlying driver remains unclear.",
        "没有匹配到可支持机制结论的事件证据，业务机制仍不明确。",
    )
    value = value.replace(
        "No event-based explanations are available due to insufficient evidence.",
        "事件解释证据不足。",
    )
    return value


def _normalize_visible_business_text(text: Any, state: WorkflowState) -> str:
    value = str(text or "")
    value = value.replace("主要驱动是", "主要贡献项是")
    value = value.replace("主要驱动力为", "主要贡献项是")
    value = value.replace("主要驱动力是", "主要贡献项是")
    value = value.replace("驱动力", "贡献项")
    value = value.replace("all_users", _scope_label("all_users"))
    value = value.replace("monthly_daily_avg_paid_amount", "月度日均付费金额")
    value = value.replace("monthly_avg_paid_amount", "月度日均付费金额")
    value = value.replace("方向命中率", "方向一致比例")
    value = value.replace("重要性命中率", "达到重要性阈值的比例")
    primary = _primary_business_evidence(state)
    capability = primary.get("capability_id") or primary.get("capability")
    if capability == "driver_decomposition":
        decomp = (primary.get("typed_payload", {}).get("decompositions") or [{}])[0]
        value = value.replace("单用户/单订单价值", _driver_unit_value_label(decomp))
        value = value.replace("单用户付费金额", _driver_unit_value_label(decomp))
        value = value.replace("用户数/订单量", _driver_volume_label(decomp))
        value = value.replace("paid_users", "付费人数")
        value = value.replace("paid_frequency", "付费频次")
        value = value.replace("avg_order_amount", "单笔付费金额")
        value = value.replace("payment_success_rate", "支付成功率")
        value = value.replace("assumed_neutral", "按不变处理")
    return value


def _format_percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "unknown"


_LLM_NODE_NAMES = frozenset(
    {
        "understand_business_intent",
        "decide_question_boundary",
        "generate_clarification",
        "confirm_business_understanding",
        "design_analysis_route",
        "repair_analysis_route",
        "interpret_data_coverage",
        "decide_next_action",
        "promotion_direction",
        "interpret_evidence",
        "audit_causal_implications",
        "synthesize_answer",
        "semantic_audit",
        "repair_answer",
        "final_business_summary",
        "generate_degraded_explanation",
        "generate_blocked_explanation",
    }
)

_BUSINESS_LABELS = {
    "understand_business_intent": "理解用户业务意图",
    "decide_question_boundary": "判断问题边界是否清楚",
    "clarification_policy_gate": "澄清策略门禁",
    "generate_clarification": "生成澄清问题",
    "rebind_after_clarification": "按用户选择重绑意图",
    "confirm_business_understanding": "确认本次业务理解",
    "design_analysis_route": "设计分析路线",
    "accept_analysis_route": "验收分析路线",
    "repair_analysis_route": "修正分析路线",
    "inspect_schema": "确认数据口径",
    "validate_runtime_binding": "数据口径与安全验收",
    "interpret_data_coverage": "解释数据覆盖的业务影响",
    "execute_capabilities": "执行已接受分析路径",
    "reduce_evidence": "整理证据简报",
    "build_diagnostic_insights": "形成诊断洞察与停止判断",
    "decide_next_action": "判断下一步分析动作",
    "promotion_direction": "提出组合归因方向",
    "promotion_policy_gate": "组合归因门禁",
    "execute_joint_attribution": "执行组合归因",
    "interpret_evidence": "解释证据和业务含义",
    "audit_causal_implications": "审计因果和业务含义",
    "synthesize_answer": "生成业务答案草稿",
    "semantic_audit": "语义审计答案",
    "hard_verify_answer": "答案硬验收",
    "repair_answer": "按校验反馈修答案",
    "final_business_summary": "整理最终业务总结",
    "generate_degraded_explanation": "生成降级说明",
    "generate_blocked_explanation": "生成阻断说明",
    "persist_artifact": "保存审计结果并返回draft",
}
