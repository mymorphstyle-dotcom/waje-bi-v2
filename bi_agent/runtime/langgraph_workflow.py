from dataclasses import dataclass
import warnings
from typing import Any, Optional, TypedDict

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

from bi_agent.capabilities.data_quality_check import data_quality_check
from bi_agent.capabilities.event_evidence import event_evidence
from bi_agent.capabilities.formula_decompose import formula_decompose
from bi_agent.capabilities.joint_attribution import joint_attribution
from bi_agent.capabilities.outlier_scan import outlier_scan
from bi_agent.capabilities.pattern_scan import scan_pattern
from bi_agent.capabilities.segment_bridge import segment_bridge
from bi_agent.runtime.answer_package import build_answer_package
from bi_agent.runtime.artifacts import persist_artifact, to_jsonable
from bi_agent.runtime.compiler import compile_graph
from bi_agent.runtime.llm_client import OpenAICompatibleLLMClient
from bi_agent.runtime.llm_prompts import build_prompt
from bi_agent.runtime.sql_safety import validate_select_only


NON_RETRYABLE_FAILURE_TYPES = frozenset(
    {"business", "evidence", "permission", "contract", "sql", "llm"}
)
LLM_REQUIRED_TASKS = (
    "business_intent",
    "boundary_decision",
    "confirm_understanding",
    "analysis_route",
    "data_coverage_interpretation",
    "next_action",
    "evidence_interpretation",
    "answer_synthesis",
    "semantic_audit",
)


class WorkflowState(TypedDict, total=False):
    request: dict[str, Any]
    run_id: str
    checkpoint_events: list[dict[str, Any]]
    validator_results: list[dict[str, Any]]
    llm_calls: list[dict[str, Any]]
    llm_client: Any
    intent: dict[str, Any]
    boundary_decision: dict[str, Any]
    clarification_outcome: dict[str, Any]
    confirmed_understanding: dict[str, Any]
    analysis_route: dict[str, Any]
    repair_attempts: int
    answer_repair_attempts: int
    compiled_graph: Any
    schema: dict[str, Any]
    sql_text: str
    sql_hash: str
    coverage_interpretation: dict[str, Any]
    evidence: list[dict[str, Any]]
    evidence_brief: dict[str, Any]
    next_action: dict[str, Any]
    evidence_interpretation: dict[str, Any]
    answer_text: str
    draft_claims: list[dict[str, Any]]
    semantic_audit: dict[str, Any]
    verifier: dict[str, Any]
    final_explanation: dict[str, Any]
    answer_package: dict[str, Any]
    artifact_path: str


@dataclass(frozen=True)
class WorkflowRunResult:
    status: str
    run_id: str
    answer_package: Optional[dict[str, Any]] = None
    artifact_path: str = ""
    failure_reason: str = ""
    checkpoint_events: tuple[dict[str, Any], ...] = ()


class WorkflowFailure(Exception):
    def __init__(self, message: str, *, failure_type: str = "technical"):
        super().__init__(message)
        self.failure_type = failure_type


def run_pattern_workflow(request: Optional[dict[str, Any]] = None) -> WorkflowRunResult:
    request = dict(request or {})
    state: WorkflowState = {
        "request": request,
        "run_id": request.get("run_id") or "phase4-draft",
        "checkpoint_events": [],
        "validator_results": [],
        "llm_calls": [],
        "repair_attempts": 0,
        "answer_repair_attempts": 0,
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
                failure_reason=f"llm_binding_failed:{exc}",
                checkpoint_events=tuple(state["checkpoint_events"]),
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
            failure_reason=str(exc),
            checkpoint_events=tuple(state["checkpoint_events"]),
        )
    except Exception as exc:
        return WorkflowRunResult(
            status="failed",
            run_id=state["run_id"],
            failure_reason=f"langgraph_execution_failed:{exc}",
            checkpoint_events=tuple(state["checkpoint_events"]),
        )

    return WorkflowRunResult(
        status="draft",
        run_id=output["run_id"],
        answer_package=output["answer_package"],
        artifact_path=output["artifact_path"],
        checkpoint_events=tuple(output["checkpoint_events"]),
    )


def build_pattern_graph():
    graph = StateGraph(WorkflowState)
    for node, func in (
        ("understand_business_intent", _understand_business_intent),
        ("decide_question_boundary", _decide_question_boundary),
        ("clarification_policy_gate", _clarification_policy_gate),
        ("generate_clarification", _generate_clarification),
        ("rebind_after_clarification", _rebind_after_clarification),
        ("confirm_business_understanding", _confirm_business_understanding),
        ("design_analysis_route", _design_analysis_route),
        ("accept_analysis_route", _accept_analysis_route),
        ("repair_analysis_route", _repair_analysis_route),
        ("inspect_schema", _inspect_schema),
        ("validate_runtime_binding", _validate_runtime_binding),
        ("interpret_data_coverage", _interpret_data_coverage),
        ("execute_capabilities", _execute_capabilities),
        ("reduce_evidence", _reduce_evidence),
        ("decide_next_action", _decide_next_action),
        ("promotion_direction", _promotion_direction),
        ("promotion_policy_gate", _promotion_policy_gate),
        ("execute_joint_attribution", _execute_joint_attribution),
        ("interpret_evidence", _interpret_evidence),
        ("synthesize_answer", _synthesize_answer),
        ("semantic_audit", _semantic_audit),
        ("sanitize_answer", _sanitize_answer),
        ("hard_verify_answer", _hard_verify_answer),
        ("repair_answer", _repair_answer),
        ("generate_degraded_explanation", _generate_degraded_explanation),
        ("generate_blocked_explanation", _generate_blocked_explanation),
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
            "ask": "generate_clarification",
            "block": "generate_blocked_explanation",
        },
    )
    graph.add_conditional_edges(
        "generate_clarification",
        _route_after_clarification,
        {"rebind": "rebind_after_clarification", "block": "generate_blocked_explanation"},
    )
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
    graph.add_edge("validate_runtime_binding", "interpret_data_coverage")
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
    graph.add_edge("reduce_evidence", "decide_next_action")
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
    graph.add_edge("interpret_evidence", "synthesize_answer")
    graph.add_edge("synthesize_answer", "semantic_audit")
    graph.add_conditional_edges(
        "semantic_audit",
        _route_after_semantic_audit,
        {
            "verify": "hard_verify_answer",
            "repair": "repair_answer",
            "sanitize": "sanitize_answer",
            "degrade": "generate_degraded_explanation",
        },
    )
    graph.add_edge("sanitize_answer", "hard_verify_answer")
    graph.add_conditional_edges(
        "hard_verify_answer",
        _route_after_hard_verify,
        {
            "passed": "persist_artifact",
            "repair": "repair_answer",
            "ask": "generate_clarification",
            "degrade": "generate_degraded_explanation",
        },
    )
    graph.add_edge("repair_answer", "semantic_audit")
    graph.add_edge("generate_degraded_explanation", "persist_artifact")
    graph.add_edge("generate_blocked_explanation", "persist_artifact")
    graph.add_edge("persist_artifact", END)
    return graph.compile()


def _retrying_node(node_name, func):
    def run(state: WorkflowState) -> WorkflowState:
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            event = _checkpoint(state, node_name, attempt)
            try:
                result = func(state)
                event["status"] = "completed"
                return result
            except WorkflowFailure as exc:
                event["status"] = "failed"
                event["failure_type"] = exc.failure_type
                event["reason"] = str(exc)
                if (
                    exc.failure_type not in NON_RETRYABLE_FAILURE_TYPES
                    and attempt < max_attempts
                ):
                    event["status"] = "retrying"
                    continue
                raise
            except Exception as exc:
                event["status"] = "failed"
                event["failure_type"] = "technical"
                event["reason"] = str(exc)
                if attempt < max_attempts:
                    event["status"] = "retrying"
                    continue
                raise WorkflowFailure(str(exc), failure_type="technical")
        return state

    return run


def _understand_business_intent(state: WorkflowState) -> WorkflowState:
    request = state["request"]
    if request.get("force_langgraph_failure"):
        raise RuntimeError("forced_langgraph_failure")
    _maybe_force_node_failure(state, "understand_business_intent")
    output = _invoke_llm(
        state,
        "business_intent",
        {
            "question": request.get("question", "Explain the paid amount pattern."),
            "question_family_hint": request.get("question_family", "pattern_explanation"),
            "target_metric_hint": request.get("target_metric", "paid_amount"),
            "pattern_family_hint": request.get("pattern_family", "intra_period"),
            "scope_hint": request.get("scope", "full_sample"),
            "time_window_hint": request.get("time_window", "2024-01..2026-05"),
        },
    )
    state["intent"] = {
        "question_family": request.get("question_family")
        or output.get("question_family")
        or "pattern_explanation",
        "target_metric": request.get("target_metric")
        or output.get("target_metric")
        or "paid_amount",
        "pattern_family": request.get("pattern_family")
        or output.get("pattern_family")
        or "intra_period",
        "pattern_params": dict(request.get("pattern_params", {})),
        "scope": request.get("scope") or output.get("scope") or "full_sample",
        "time_window": request.get("time_window")
        or output.get("time_window")
        or "2024-01..2026-05",
        "target_claim": output.get("target_claim", "pattern_explanation"),
        "baseline_candidates": list(output.get("baseline_candidates", [])),
        "requested_nodes": tuple(request.get("requested_nodes", ())),
    }
    return state


def _decide_question_boundary(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "decide_question_boundary")
    state["boundary_decision"] = _invoke_llm(
        state,
        "boundary_decision",
        {
            "intent": state["intent"],
            "available_defaults": {
                "scope": state["intent"]["scope"],
                "time_window": state["intent"]["time_window"],
                "pattern_family": state["intent"]["pattern_family"],
            },
            "phase4_policy": "ask only when ambiguity can change conclusion, baseline, time semantics, permission, claim strength, or cost",
        },
    )
    return state


def _clarification_policy_gate(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "clarification_policy_gate")
    decision = state["boundary_decision"]
    status = decision.get("boundary_status", "clear")
    if status not in {"clear", "low_risk_assumption", "needs_question", "cannot_answer"}:
        status = "needs_question"
    if status == "needs_question" and not state["request"].get("allow_question_interrupt", True):
        status = "low_risk_assumption"
    state["clarification_outcome"] = {
        "status": "pending" if status == "needs_question" else "system_inferred",
        "boundary_status": status,
        "recommended_assumption": decision.get("recommended_assumption"),
    }
    _current_event(state)["route"] = status
    return state


def _generate_clarification(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "generate_clarification")
    choice = state["request"].get("clarification_choice")
    output = _invoke_llm(
        state,
        "clarification_question",
        {
            "intent": state.get("intent", {}),
            "boundary_decision": state.get("boundary_decision", {}),
            "clarification_choice": choice,
        },
    )
    state["clarification_outcome"] = {
        "status": "user_selected" if choice else "question_tool_opened",
        "questions": output.get("questions")
        or state["boundary_decision"].get("clarification_questions", []),
        "recommended_assumption": output.get("recommended_assumption")
        or state["boundary_decision"].get("recommended_assumption"),
        "choice": choice,
    }
    return state


def _rebind_after_clarification(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "rebind_after_clarification")
    choice = state["clarification_outcome"].get("choice", {})
    if isinstance(choice, dict):
        state["intent"].update({k: v for k, v in choice.items() if v})
    return state


def _confirm_business_understanding(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "confirm_business_understanding")
    state["confirmed_understanding"] = _invoke_llm(
        state,
        "confirm_understanding",
        {
            "intent": state["intent"],
            "boundary_decision": state["boundary_decision"],
            "clarification_outcome": state.get("clarification_outcome", {}),
        },
    )
    return state


def _design_analysis_route(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "design_analysis_route")
    request = state["request"]
    output = _invoke_llm(
        state,
        "analysis_route",
        {
            "intent": state["intent"],
            "confirmed_understanding": state["confirmed_understanding"],
            "known_capabilities": (
                "data_quality_check",
                "pattern_scan",
                "formula_decompose",
                "event_evidence",
                "segment_bridge",
                "outlier_scan",
                "joint_attribution",
                "answer_verify",
            ),
            "requested_nodes_hint": tuple(request.get("requested_nodes", ())),
        },
    )
    requested = tuple(output.get("requested_nodes") or request.get("requested_nodes", ()))
    if not requested:
        requested = ("pattern_scan",)
    state["analysis_route"] = {**output, "requested_nodes": requested}
    state["intent"]["requested_nodes"] = requested
    return state


def _accept_analysis_route(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "accept_analysis_route")
    intent = state["intent"]
    compiled = compile_graph(
        question_family=intent["question_family"],
        target_metric=intent["target_metric"],
        pattern_family=intent["pattern_family"],
        requested_nodes=intent["requested_nodes"],
    )
    state["compiled_graph"] = compiled
    return state


def _repair_analysis_route(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "repair_analysis_route")
    state["repair_attempts"] = state.get("repair_attempts", 0) + 1
    output = _invoke_llm(
        state,
        "route_repair",
        {
            "intent": state["intent"],
            "analysis_route": state["analysis_route"],
            "compiler_feedback": to_jsonable(state["compiled_graph"].mutations.records),
            "repair_attempt": state["repair_attempts"],
        },
    )
    requested = tuple(output.get("requested_nodes") or ("pattern_scan",))
    state["analysis_route"] = {**state["analysis_route"], **output, "requested_nodes": requested}
    state["intent"]["requested_nodes"] = requested
    return state


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
            "validator": "permission",
            "ok": True,
            "reason": "aggregate_only",
        },
    ]
    state["validator_results"] = validator_results
    if not sql_result.ok:
        raise WorkflowFailure(sql_result.reason, failure_type="sql")
    return state


def _interpret_data_coverage(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "interpret_data_coverage")
    state["coverage_interpretation"] = _invoke_llm(
        state,
        "data_coverage_interpretation",
        {
            "intent": state["intent"],
            "schema_summary": state["schema"],
            "validator_results": state["validator_results"],
            "sql_hash": state["sql_hash"],
        },
    )
    return state


def _execute_capabilities(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "execute_capabilities")
    rows = state["request"].get("rows") or _default_pattern_rows()
    query_ref = (state["sql_hash"],)
    evidence = []
    compiled = state["compiled_graph"]
    capabilities = tuple(compiled.mutations.accepted_graph)

    if "data_quality_check" in capabilities:
        evidence.append(
            data_quality_check(
                rows,
                required_fields=tuple(
                    state["request"].get("required_fields", ("month", "phase", "amount"))
                ),
                result_refs=query_ref,
            )
        )
    if "pattern_scan" in capabilities:
        pattern_family = state["intent"]["pattern_family"]
        pattern_params = dict(state["intent"].get("pattern_params", {}))
        if pattern_family == "intra_period":
            pattern_params.setdefault("target_phase", "start")
        evidence.append(
            scan_pattern(
                rows,
                pattern_family=pattern_family,
                materiality_floor=0.03,
                result_refs=query_ref,
                evidence_ref=f"pattern_scan:{pattern_family}",
                **pattern_params,
            )
        )
    if "formula_decompose" in capabilities:
        evidence.append(
            formula_decompose(
                [{"formula_id": "paid_amount", "components": ("paid_amount",)}],
                available_components=("paid_amount",),
                result_refs=query_ref,
            )
        )
    if "event_evidence" in capabilities:
        evidence.append(event_evidence(state["request"].get("events", ()), result_refs=query_ref))
    if "segment_bridge" in capabilities:
        segment = segment_bridge(
            state["request"].get(
                "segments",
                ({"segment": "full_sample", "amount": 1.0, "n": 100},),
            ),
            result_refs=query_ref,
        )
        evidence.append(segment)
    else:
        segment = None
    if "outlier_scan" in capabilities:
        evidence.append(outlier_scan(rows, result_refs=query_ref))
    if "joint_attribution" in capabilities:
        evidence.append(joint_attribution(segment_evidence=segment, result_refs=query_ref))

    state["evidence"] = [_evidence_dict(item, state) for item in evidence]
    return state


def _reduce_evidence(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "reduce_evidence")
    pattern_ref = f"pattern_scan:{state['intent']['pattern_family']}"
    pattern = _evidence_by_ref(state.get("evidence", [])).get(pattern_ref, {})
    state["evidence_brief"] = {
        "pattern_ref": pattern_ref,
        "pattern_status": pattern.get("strength", "insufficient"),
        "pattern_established": bool(pattern.get("established")),
        "wording_limit": pattern.get("wording_limit", "unknown"),
        "limitations": sorted(
            {
                limitation
                for item in state.get("evidence", [])
                for limitation in item.get("limitations", ())
            }
        ),
        "evidence_refs": [item.get("evidence_ref") for item in state.get("evidence", [])],
    }
    return state


def _decide_next_action(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "decide_next_action")
    state["next_action"] = _invoke_llm(
        state,
        "next_action",
        {
            "intent": state["intent"],
            "accepted_graph": to_jsonable(state["compiled_graph"].mutations.accepted_graph),
            "evidence_brief": state["evidence_brief"],
            "allow_question_interrupt": state["request"].get("allow_question_interrupt", True),
        },
    )
    return state


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
    requested = tuple(state.get("analysis_route", {}).get("promotion", {}).get("requested_nodes", ()))
    if "joint_attribution" in requested:
        _current_event(state)["route"] = "accepted"
    else:
        _current_event(state)["route"] = "degraded_or_skip"
    return state


def _execute_joint_attribution(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "execute_joint_attribution")
    evidence = list(state.get("evidence", []))
    segment = next((item for item in evidence if item.get("capability") == "segment_bridge"), None)
    evidence.append(
        _evidence_dict(
            joint_attribution(segment_evidence=segment, result_refs=(state["sql_hash"],)),
            state,
        )
    )
    state["evidence"] = evidence
    return state


def _interpret_evidence(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "interpret_evidence")
    state["evidence_interpretation"] = _invoke_llm(
        state,
        "evidence_interpretation",
        {
            "intent": state["intent"],
            "evidence_brief": state["evidence_brief"],
            "evidence": state["evidence"],
        },
    )
    return state


def _synthesize_answer(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "synthesize_answer")
    output = _invoke_llm(
        state,
        "answer_synthesis",
        {
            "intent": state["intent"],
            "evidence_interpretation": state["evidence_interpretation"],
            "evidence_brief": state["evidence_brief"],
            "evidence": state["evidence"],
            "evidence_refs": [item.get("evidence_ref") for item in state["evidence"]],
        },
    )
    state["answer_text"] = _weaken_unsupported_causal_wording(output.get("answer_text", ""))
    state["draft_claims"] = state["request"].get("draft_claims") or _claims_from_llm_or_default(
        output.get("claims"),
        state,
    )
    if _single_period_pattern(state):
        state["answer_text"] = state["draft_claims"][0]["text"]
    return state


def _semantic_audit(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "semantic_audit")
    state["semantic_audit"] = _invoke_llm(
        state,
        "semantic_audit",
        {
            "answer_text": state.get("answer_text", ""),
            "draft_claims": state["draft_claims"],
            "evidence_brief": state["evidence_brief"],
            "wording_boundary": "causal and main-driver wording require explicit supporting evidence",
        },
    )
    return state


def _sanitize_answer(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "sanitize_answer")
    previous = state.get("semantic_audit", {})
    _sanitize_to_bounded_pattern_answer(state)
    state["semantic_audit"] = {
        **previous,
        "audit_status": "sanitized",
        "sanitized_by": "local_bounded_pattern_policy",
    }
    return state


def _hard_verify_answer(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "hard_verify_answer")
    package = _build_answer_package_from_state(state)
    verifier = package["admin_audit"]["verifier"]
    state["verifier"] = verifier
    return state


def _repair_answer(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "repair_answer")
    state["answer_repair_attempts"] = state.get("answer_repair_attempts", 0) + 1
    output = _invoke_llm(
        state,
        "answer_repair",
        {
            "answer_text": state.get("answer_text", ""),
            "draft_claims": state["draft_claims"],
            "semantic_audit": state["semantic_audit"],
            "verifier": state.get("verifier", {}),
            "evidence_brief": state["evidence_brief"],
        },
    )
    state["answer_text"] = _weaken_unsupported_causal_wording(
        output.get("answer_text", state.get("answer_text", ""))
    )
    state["draft_claims"] = _claims_from_llm_or_default(output.get("claims"), state)
    if _single_period_pattern(state):
        state["answer_text"] = state["draft_claims"][0]["text"]
    return state


def _generate_degraded_explanation(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "generate_degraded_explanation")
    state["final_explanation"] = _invoke_llm(
        state,
        "degraded_explanation",
        {
            "intent": state.get("intent", {}),
            "evidence_brief": state.get("evidence_brief", {}),
            "verifier": state.get("verifier", {}),
        },
    )
    if "evidence" not in state:
        state["evidence"] = []
    if "draft_claims" not in state:
        state["draft_claims"] = []
    return state


def _generate_blocked_explanation(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "generate_blocked_explanation")
    if "final_explanation" not in state:
        state["final_explanation"] = _invoke_llm(
            state,
            "blocked_explanation",
            {
                "intent": state.get("intent", {}),
                "boundary_decision": state.get("boundary_decision", {}),
                "validator_results": state.get("validator_results", []),
            },
        )
    if "evidence" not in state:
        state["evidence"] = []
    if "draft_claims" not in state:
        state["draft_claims"] = []
    return state


def _persist_artifact(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "persist_artifact")
    _current_event(state)["status"] = "completed"
    package = _build_answer_package_from_state(state)
    artifact_path = persist_artifact(
        package,
        artifact_root=state["request"].get("artifact_root", "artifacts/phase-4"),
    )
    state["answer_package"] = package
    state["artifact_path"] = artifact_path
    return state


def _route_after_clarification_policy(state: WorkflowState) -> str:
    status = state["clarification_outcome"].get("boundary_status")
    if status == "needs_question":
        return "ask"
    if status == "cannot_answer":
        return "block"
    return "confirm"


def _route_after_clarification(state: WorkflowState) -> str:
    return "rebind" if state["clarification_outcome"].get("choice") else "block"


def _route_after_accept_analysis(state: WorkflowState) -> str:
    compiled = state["compiled_graph"]
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
        return "degrade"
    return "sufficient"


def _route_after_next_action(state: WorkflowState) -> str:
    action = state["next_action"].get("next_action", "synthesize_answer")
    if action in {"continue_evidence", "scan_sibling"}:
        prior_plan_count = sum(
            1
            for event in state.get("checkpoint_events", ())
            if event.get("node") == "decide_next_action" and event.get("route") == "plan"
        )
        if prior_plan_count < 1:
            _current_event(state)["route"] = "plan"
            return "plan"
        _current_event(state)["route"] = "synthesize_after_loop_cap"
        return "synthesize"
    if action == "ask_question":
        if state["request"].get("allow_question_interrupt", True):
            return "ask"
        return "synthesize" if _pattern_supports_bounded_answer(state) else "degrade"
    if action == "promote_attribution":
        return "promote"
    if action == "degrade":
        if _pattern_supports_bounded_answer(state):
            _current_event(state)["route"] = "degrade_overridden_to_bounded_answer"
            return "synthesize"
        return "degrade"
    return "synthesize" if _pattern_supports_bounded_answer(state) else "degrade"


def _route_after_promotion_policy(state: WorkflowState) -> str:
    route = _current_event(state).get("route")
    if route == "accepted":
        return "accepted"
    return "synthesize" if _pattern_supports_bounded_answer(state) else "degrade"


def _route_after_hard_verify(state: WorkflowState) -> str:
    verifier = state.get("verifier", {})
    if not verifier.get("errors"):
        return "passed"
    if state.get("answer_repair_attempts", 0) < 1:
        return "repair"
    return "degrade"


def _route_after_semantic_audit(state: WorkflowState) -> str:
    audit = state.get("semantic_audit", {})
    status = str(audit.get("audit_status", "")).lower()
    has_issues = bool(audit.get("issues"))
    if status in {"fail", "failed", "needs_revision"} or has_issues:
        if state.get("answer_repair_attempts", 0) < 1:
            return "repair"
        if _pattern_supports_bounded_answer(state):
            _current_event(state)["route"] = "semantic_sanitized_to_bounded_answer"
            return "sanitize"
        return "degrade"
    return "verify"


def _build_answer_package_from_state(state: WorkflowState) -> dict[str, Any]:
    compiled = state.get("compiled_graph")
    proposed_graph = compiled.mutations.proposed_graph if compiled else ()
    accepted_graph = compiled.mutations.accepted_graph if compiled else ()
    records = compiled.mutations.records if compiled else ()
    return build_answer_package(
        run_id=state["run_id"],
        draft_claims=state.get("draft_claims", []),
        evidence=state.get("evidence", []),
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
        },
        llm_calls=state.get("llm_calls", []),
        semantic_audit=state.get("semantic_audit", {}),
        final_explanation=state.get("final_explanation", {}),
        answer_text=state.get("answer_text", ""),
        coverage_interpretation=state.get("coverage_interpretation", {}),
        clarification_outcome=state.get("clarification_outcome", {}),
    )


def _invoke_llm(state: WorkflowState, task: str, payload: dict[str, Any]) -> dict[str, Any]:
    spec = build_prompt(task, payload)
    result = state["llm_client"].invoke_json(
        task=spec.task,
        prompt_version=spec.prompt_version,
        messages=spec.messages,
        required_keys=spec.required_keys,
    )
    state["llm_calls"].append(result.audit)
    return result.output


def _checkpoint(state: WorkflowState, node_name: str, attempt: int) -> dict[str, Any]:
    event = {
        "node": node_name,
        "attempt": attempt,
        "status": "running",
        "label": _BUSINESS_LABELS.get(node_name, node_name),
        "llm": node_name in _LLM_NODE_NAMES,
    }
    state["checkpoint_events"].append(event)
    return event


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
    payload = dict(evidence.get("typed_payload", {}))
    payload["scope"] = state["intent"]["scope"]
    payload["time_window"] = state["intent"]["time_window"]
    evidence["typed_payload"] = payload
    return evidence


def _evidence_by_ref(evidence: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["evidence_ref"]: item for item in evidence}


def _pattern_supports_bounded_answer(state: WorkflowState) -> bool:
    pattern_ref = f"pattern_scan:{state['intent']['pattern_family']}"
    pattern = _evidence_by_ref(state.get("evidence", [])).get(pattern_ref, {})
    if pattern.get("established"):
        return True
    return pattern.get("strength") in {"high", "medium"} and pattern.get("wording_limit") in {
        "supported",
        "candidate_mechanism_only",
    }


def _sanitize_to_bounded_pattern_answer(state: WorkflowState) -> None:
    claim = _default_claim_from_evidence(state)
    state["draft_claims"] = [claim]
    state["answer_text"] = claim["text"]


def _single_period_pattern(state: WorkflowState) -> bool:
    pattern_ref = f"pattern_scan:{state['intent']['pattern_family']}"
    pattern = _evidence_by_ref(state.get("evidence", [])).get(pattern_ref, {})
    try:
        return int(pattern.get("typed_payload", {}).get("comparable_periods", 0)) <= 1
    except (TypeError, ValueError):
        return False


def _claims_from_llm_or_default(claims: Any, state: WorkflowState) -> list[dict[str, Any]]:
    _ = claims
    return [_default_claim_from_evidence(state)]


def _default_claim_from_evidence(state: WorkflowState) -> dict[str, Any]:
    pattern_family = state["intent"]["pattern_family"]
    pattern = _evidence_by_ref(state.get("evidence", [])).get(f"pattern_scan:{pattern_family}", {})
    payload = pattern.get("typed_payload", {})
    median_uplift = payload.get("median_uplift")
    comparable_periods = payload.get("comparable_periods")
    direction_ratio = payload.get("direction_ratio")
    limitation_text = (
        " Mechanism evidence is unavailable."
        if "no_event_contract_or_matches" in state.get("evidence_brief", {}).get("limitations", ())
        else ""
    )
    period_label = "comparable period" if comparable_periods == 1 else "comparable periods"
    return {
        "text": (
            f"{_pattern_label(pattern_family)} is observed in {state['intent']['time_window']} "
            f"with median uplift {_format_percent(median_uplift)}, "
            f"direction ratio {_format_percent(direction_ratio)}, and "
            f"{comparable_periods} {period_label}.{limitation_text}"
        ),
        "evidence_refs": [f"pattern_scan:{pattern_family}"],
        "numbers": {
            "median_uplift": median_uplift,
            "comparable_periods": comparable_periods,
            "direction_ratio": direction_ratio,
        },
        "scope": state["intent"]["scope"],
        "time_window": state["intent"]["time_window"],
    }


def _pattern_label(pattern_family: str) -> str:
    return {
        "weekly": "Weekly paid amount pattern",
        "rolling": "Rolling paid amount pattern",
        "custom_baseline": "Custom-baseline paid amount comparison",
        "intra_period": "Intra-period paid amount pattern",
        "event_relative": "Event-relative paid amount pattern",
        "lag_recovery": "Lag/recovery paid amount pattern",
    }.get(pattern_family, f"{pattern_family} paid amount pattern")


def _weaken_unsupported_causal_wording(text: Any) -> str:
    value = str(text or "")
    value = value.replace(
        "No event-based causes were identified to explain the pattern.",
        "No event-based mechanism evidence was identified for the pattern.",
    )
    value = value.replace(
        "No causal events were matched, so the pattern's underlying driver remains unclear.",
        "No event-based mechanism evidence was matched, so the business mechanism remains unclear.",
    )
    value = value.replace(
        "No event-based explanations are available due to insufficient evidence.",
        "Event-based explanation evidence is insufficient.",
    )
    return value


def _format_percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "unknown"


def _default_pattern_rows() -> list[dict[str, Any]]:
    rows = []
    year = 2024
    month = 1
    while (year, month) <= (2026, 5):
        month_key = f"{year}-{month:02d}"
        rows.extend(
            [
                {"month": month_key, "phase": "start", "amount": 120},
                {"month": month_key, "phase": "mid", "amount": 100},
                {"month": month_key, "phase": "end", "amount": 100},
            ]
        )
        month += 1
        if month == 13:
            year += 1
            month = 1
    return rows


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
        "synthesize_answer",
        "semantic_audit",
        "repair_answer",
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
    "decide_next_action": "判断下一步分析动作",
    "promotion_direction": "提出组合归因方向",
    "promotion_policy_gate": "组合归因门禁",
    "execute_joint_attribution": "执行组合归因",
    "interpret_evidence": "解释证据和业务含义",
    "synthesize_answer": "生成业务答案草稿",
    "semantic_audit": "语义审计答案",
    "sanitize_answer": "收敛为有边界答案",
    "hard_verify_answer": "答案硬验收",
    "repair_answer": "按校验反馈修答案",
    "generate_degraded_explanation": "生成降级说明",
    "generate_blocked_explanation": "生成阻断说明",
    "persist_artifact": "保存审计结果并返回draft",
}
