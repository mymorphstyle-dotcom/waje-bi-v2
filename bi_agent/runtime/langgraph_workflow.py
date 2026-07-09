from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from time import perf_counter
import warnings
from typing import Any, Iterable, Mapping, Optional, Sequence, TypedDict

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
from bi_agent.capabilities.driver_decomposition import driver_decomposition
from bi_agent.capabilities.event_evidence import event_evidence
from bi_agent.capabilities.formula_decompose import formula_decompose
from bi_agent.capabilities.high_value_user_contribution import high_value_user_contribution
from bi_agent.capabilities.joint_attribution import joint_attribution
from bi_agent.capabilities.outlier_scan import outlier_scan
from bi_agent.capabilities.outlier_contribution import outlier_contribution
from bi_agent.capabilities.pattern_scan import scan_pattern
from bi_agent.capabilities.segment_contribution import segment_contribution
from bi_agent.capabilities.segment_bridge import segment_bridge
from bi_agent.capabilities.user_mix_contribution import user_mix_contribution
from bi_agent.runtime.answer_package import build_answer_package
from bi_agent.runtime.artifacts import persist_artifact, to_jsonable
from bi_agent.runtime.capability_harness import (
    PATTERN_COMPARE_CAPABILITIES,
    execute_capability,
)
from bi_agent.runtime.capability_models import CapabilityRequest
from bi_agent.runtime.capability_registry import llm_capability_cards
from bi_agent.runtime.compiler import compile_graph
from bi_agent.runtime.data_contract_diagnostics import (
    contract_fields_from_records,
    diagnose_contract_gaps,
)
from bi_agent.runtime.exploration_budget import default_budget, record_capability_call
from bi_agent.runtime.llm_client import OpenAICompatibleLLMClient
from bi_agent.runtime.llm_prompts import build_prompt
from bi_agent.runtime.sql_safety import validate_select_only
from bi_agent.runtime.wording import CAUSAL_WORDING, SINGLE_PERIOD_CONFIDENCE


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
    "causal_audit",
    "answer_synthesis",
    "semantic_audit",
)
ROUTE_BLOCKED_CAPABILITY_IDS = frozenset(
    {
        "evidence_reduce",
        "metric_coverage_profile",
        "metric_timeseries",
        "component_contribution",
        "segment_breakdown",
        "segment_shift_compare",
        "candidate_dimension_screen",
        "change_point_scan",
    }
)


class WorkflowState(TypedDict, total=False):
    request: dict[str, Any]
    run_id: str
    checkpoint_events: list[dict[str, Any]]
    validator_results: list[dict[str, Any]]
    llm_calls: list[dict[str, Any]]
    llm_client: Any
    budget_state: Any
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
    row_query_plan: dict[str, Any]
    runtime_rows_by_intent: dict[str, list[dict[str, Any]]]
    coverage_interpretation: dict[str, Any]
    evidence: list[dict[str, Any]]
    evidence_brief: dict[str, Any]
    next_action: dict[str, Any]
    evidence_interpretation: dict[str, Any]
    causal_evidence_dossier: dict[str, Any]
    causal_audit: dict[str, Any]
    answer_text: str
    draft_claims: list[dict[str, Any]]
    semantic_audit: dict[str, Any]
    verifier: dict[str, Any]
    retry_context: dict[str, Any]
    final_explanation: dict[str, Any]
    final_business_summary: str
    final_answer_audit: dict[str, Any]
    final_summary_display_warnings: list[str]
    quality_gate: dict[str, Any]
    follow_up_questions: list[str]
    answer_package: dict[str, Any]
    artifact_path: str
    contract_gap_diagnostics: tuple[dict[str, Any], ...]


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
        ("fetch_runtime_rows", _fetch_runtime_rows),
        ("interpret_data_coverage", _interpret_data_coverage),
        ("execute_capabilities", _execute_capabilities),
        ("reduce_evidence", _reduce_evidence),
        ("decide_next_action", _decide_next_action),
        ("promotion_direction", _promotion_direction),
        ("promotion_policy_gate", _promotion_policy_gate),
        ("execute_joint_attribution", _execute_joint_attribution),
        ("interpret_evidence", _interpret_evidence),
        ("audit_causal_implications", _audit_causal_implications),
        ("synthesize_answer", _synthesize_answer),
        ("semantic_audit", _semantic_audit),
        ("sanitize_answer", _sanitize_answer),
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
    graph.add_conditional_edges(
        "validate_runtime_binding",
        _route_after_runtime_binding,
        {
            "valid": "fetch_runtime_rows",
            "block": "generate_blocked_explanation",
        },
    )
    graph.add_conditional_edges(
        "fetch_runtime_rows",
        _route_after_runtime_rows,
        {
            "valid": "interpret_data_coverage",
            "block": "generate_blocked_explanation",
        },
    )
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
    graph.add_edge("interpret_evidence", "audit_causal_implications")
    graph.add_edge("audit_causal_implications", "synthesize_answer")
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
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            started = perf_counter()
            event = _checkpoint(state, node_name, attempt)
            try:
                result = func(state)
                _finish_checkpoint(event, "completed", started)
                if node_name == "persist_artifact":
                    _refresh_persisted_answer_package(result)
                return result
            except WorkflowFailure as exc:
                event["failure_type"] = exc.failure_type
                event["reason"] = str(exc)
                if (
                    exc.failure_type not in NON_RETRYABLE_FAILURE_TYPES
                    and attempt < max_attempts
                ):
                    _finish_checkpoint(event, "retrying", started)
                    continue
                _finish_checkpoint(event, "failed", started)
                raise
            except Exception as exc:
                event["failure_type"] = "technical"
                event["reason"] = str(exc)
                if attempt < max_attempts:
                    _finish_checkpoint(event, "retrying", started)
                    continue
                _finish_checkpoint(event, "failed", started)
                raise WorkflowFailure(str(exc), failure_type="technical")
        return state

    return run


def _understand_business_intent(state: WorkflowState) -> WorkflowState:
    request = state["request"]
    if request.get("force_langgraph_failure"):
        raise RuntimeError("forced_langgraph_failure")
    _maybe_force_node_failure(state, "understand_business_intent")
    intent_payload = _business_intent_payload(request)
    try:
        output = _invoke_llm(state, "business_intent", intent_payload)
    except Exception as exc:
        output = _local_business_intent_fallback(state, intent_payload, exc)
    pattern_family = _normalize_pattern_family(output.get("pattern_family"), request)
    state["intent"] = _normalize_question_families({
        "question_family": output.get("question_family") or "pattern_explanation",
        "target_metric": _normalize_target_metric(
            request.get("target_metric")
            or output.get("target_metric")
            or "paid_amount"
        ),
        "pattern_family": pattern_family,
        "pattern_params": _normalize_pattern_params(request, output, pattern_family),
        "scope": _normalize_scope(
            request.get("scope") or output.get("scope") or "full_sample"
        ),
        "time_window": request.get("time_window")
        or output.get("time_window")
        or "2024-01..2026-05",
        "target_claim": _normalize_target_claim(
            output.get("target_claim", "pattern_explanation")
        ),
        "baseline_candidates": list(output.get("baseline_candidates") or []),
        "sub_intents": list(output.get("sub_intents") or []),
        "ambiguous_slots": list(output.get("ambiguous_slots") or []),
        "answer_contract": dict(output.get("answer_contract") or {}),
        "baseline": request.get("baseline") or output.get("baseline") or {},
        "target": request.get("target") or output.get("target") or {},
        "question": request.get("question", ""),
        "requested_nodes": (),
        "question_families": list(output.get("question_families") or ()),
        "primary_question_family": output.get("primary_question_family"),
        "secondary_question_families": list(output.get("secondary_question_families") or ()),
    })
    return state


def _local_business_intent_fallback(
    state: WorkflowState,
    payload: dict[str, Any],
    exc: Exception,
) -> dict[str, Any]:
    request = state["request"]
    question = str(request.get("question") or "")
    business_text = question.lower()
    question_family = "pattern_explanation"
    pattern_family = "intra_period"
    target_claim = "pattern_explanation"
    secondary_families: list[str] = []
    if _business_text_requests_change_explanation(business_text):
        question_family = "paid_amount_change_explanation"
        pattern_family = "custom_baseline"
        target_claim = "comparative_change"
    if _business_text_requests_segment_contribution(
        business_text
    ) or _business_text_requests_joint_attribution(business_text):
        if question_family != "segment_or_factor_attribution":
            secondary_families.append("segment_or_factor_attribution")
        if question_family == "pattern_explanation":
            question_family = "segment_or_factor_attribution"
            target_claim = "segment_contribution"
    if _business_text_requests_outlier_recalc(business_text):
        if question_family != "anomaly_or_black_swan_review":
            secondary_families.append("anomaly_or_black_swan_review")
        if question_family == "pattern_explanation":
            question_family = "anomaly_or_black_swan_review"
            target_claim = "external_shock_candidate_or_anomaly"
    if _business_text_requests_period_recompare(business_text):
        if question_family != "custom_baseline_comparison":
            secondary_families.append("custom_baseline_comparison")
        if question_family == "pattern_explanation":
            question_family = "custom_baseline_comparison"
            pattern_family = "custom_baseline"
            target_claim = "comparative_change"

    output = {
        "question_family": question_family,
        "primary_question_family": question_family,
        "secondary_question_families": secondary_families,
        "question_families": [question_family, *secondary_families],
        "target_metric": request.get("target_metric") or "paid_amount",
        "pattern_family": request.get("pattern_family") or pattern_family,
        "scope": request.get("scope") or "full_sample",
        "time_window": request.get("time_window") or "2024-01..2026-05",
        "target_claim": target_claim,
        "baseline_candidates": ["current_topic_baseline", "custom_baseline"],
        "sub_intents": [],
        "ambiguous_slots": [],
        "answer_contract": {},
        "baseline": request.get("baseline") or {},
        "target": request.get("target") or {},
        "status_message": "LLM 意图识别不可用，已按本地业务表达和合同边界继续。",
    }
    state["llm_calls"].append(
        _local_llm_fallback_audit(
            task="business_intent",
            payload=payload,
            output=output,
            exc=exc,
        )
    )
    return output


def _business_intent_payload(request: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "question": request.get("question", "Explain the paid amount question.")
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
    if context:
        payload["bound_business_context"] = context
    return payload


def _normalize_question_families(intent: dict[str, Any]) -> dict[str, Any]:
    primary = str(
        intent.get("primary_question_family")
        or intent.get("question_family")
        or "pattern_explanation"
    )
    families = [
        str(item)
        for item in intent.get("question_families", ())
        if item
    ]
    if primary not in families:
        families.insert(0, primary)
    secondary = [
        str(item)
        for item in intent.get("secondary_question_families", ())
        if item and str(item) != primary
    ]
    for family in families:
        if family != primary and family not in secondary:
            secondary.append(family)
    return {
        **intent,
        "question_family": primary,
        "primary_question_family": primary,
        "question_families": families,
        "secondary_question_families": secondary,
    }


def _normalize_scope(scope: Any) -> str:
    if isinstance(scope, Mapping):
        scope = (
            scope.get("value")
            or scope.get("type")
            or scope.get("label")
            or scope.get("scope")
        )
    value = str(scope or "").strip()
    aliases = {
        "all",
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
    }
    if value.lower() in aliases:
        return "full_sample"
    return value or "full_sample"


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


def _normalize_pattern_family(pattern_family: Any, request: Mapping[str, Any]) -> str:
    supported = {
        "intra_period",
        "weekly",
        "event_relative",
        "rolling",
        "lag_recovery",
        "custom_baseline",
    }
    request_value = str(request.get("pattern_family") or "").strip().lower()
    if request_value in supported:
        return request_value
    value = str(pattern_family or "").strip().lower()
    if value in supported:
        return value
    return "intra_period"


def _normalize_pattern_params(
    request: Mapping[str, Any],
    output: Mapping[str, Any],
    pattern_family: str,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    output_params = output.get("pattern_params")
    if isinstance(output_params, Mapping):
        params.update(dict(output_params))
    request_params = request.get("pattern_params")
    if isinstance(request_params, Mapping):
        params.update(dict(request_params))

    question = str(request.get("question") or "")
    if pattern_family == "weekly" and "周末" in question:
        params.setdefault("weekday_key", "weekday")
        params.setdefault("target_weekdays", [6, 7])
        params.setdefault("baseline_weekdays", [1, 2, 3, 4, 5])
    if pattern_family == "intra_period" and "月初" in question:
        params.setdefault("target_phase", "start")
    return params


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
        "available_defaults": {
            "scope": state["intent"]["scope"],
            "time_window": state["intent"]["time_window"],
            "pattern_family": state["intent"]["pattern_family"],
        },
        "phase4_policy": "ask only when ambiguity can change conclusion, baseline, time semantics, permission, claim strength, or cost",
    }
    if str(state.get("request", {}).get("question") or "").strip():
        decision = _local_question_boundary_decision(state)
        state["llm_calls"].append(
            _local_llm_decision_audit(
                task="boundary_decision",
                payload=boundary_payload,
                output=decision,
                reason="local_clarification_policy",
            )
        )
        state["boundary_decision"] = decision
        return state
    state["boundary_decision"] = _invoke_llm(state, "boundary_decision", boundary_payload)
    return state


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
    if status == "needs_question" and state["request"].get("clarification_choice"):
        status = "low_risk_assumption"
        decision = {
            **decision,
            "recommended_assumption": "已按用户澄清继续执行，并把该选择写入本次分析边界。",
        }
    if status == "needs_question" and not state["request"].get("allow_question_interrupt", True):
        status = "low_risk_assumption"
    state["clarification_outcome"] = {
        "status": "pending" if status == "needs_question" else "system_inferred",
        "boundary_status": status,
        "recommended_assumption": decision.get("recommended_assumption"),
        "choice": state["request"].get("clarification_choice"),
    }
    _current_event(state)["route"] = status
    return state


def _can_continue_with_default_business_boundary(
    state: WorkflowState, decision: Mapping[str, Any]
) -> bool:
    intent = state.get("intent", {})
    if "revenue_health_review" not in _intent_question_family_set(intent):
        return False
    if not decision.get("recommended_assumption"):
        return False
    hard_slots = {"target_metric", "metric", "time_window", "date_range", "scope", "permission"}
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
    choice = state["request"].get("clarification_choice")
    clarification_payload = {
        "intent": state.get("intent", {}),
        "boundary_decision": state.get("boundary_decision", {}),
        "clarification_choice": choice,
    }
    if str(state.get("request", {}).get("question") or "").strip():
        output = _local_clarification_question_output(state)
        state["llm_calls"].append(
            _local_llm_decision_audit(
                task="clarification_question",
                payload=clarification_payload,
                output=output,
                reason="local_clarification_options",
            )
        )
    else:
        output = _invoke_llm(state, "clarification_question", clarification_payload)
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
    }
    return state


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
    budget = state.get("budget_state") or default_budget("ordinary")
    state["budget_state"] = budget
    route_payload = {
        "intent": state["intent"],
        "confirmed_understanding": state["confirmed_understanding"],
        "known_capabilities": _route_capability_cards(),
        "budget_state": budget.to_llm_summary(),
    }
    output = _invoke_llm(state, "analysis_route", route_payload)
    requested = _requested_node_ids(
        output.get("requested_nodes"),
        excluded=ROUTE_BLOCKED_CAPABILITY_IDS,
    )
    if not requested:
        requested = ("pattern_scan",)
    output = _align_route_output_to_requested(output, requested)
    _infer_question_families_from_requested_nodes(state["intent"], requested)
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
        question_families=intent.get("question_families", ()),
        question_text=str(state["request"].get("question") or ""),
        bound_context=_compiler_bound_context(state),
        prior_analysis_assets=tuple(state["request"].get("prior_analysis_assets") or ()),
    )
    state["compiled_graph"] = compiled
    state["request"]["compiler_runtime_plan"] = compiled.runtime_plan
    _refresh_contract_gap_diagnostics(state)
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
    requested = _requested_node_ids(
        output.get("requested_nodes"),
        excluded=ROUTE_BLOCKED_CAPABILITY_IDS,
    )
    if not requested:
        requested = ("pattern_scan",)
    output = _align_route_output_to_requested(output, requested)
    _infer_question_families_from_requested_nodes(state["intent"], requested)
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
    return state


def _fetch_runtime_rows(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "fetch_runtime_rows")
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


def _interpret_data_coverage(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "interpret_data_coverage")
    coverage_payload = {
        "intent": state["intent"],
        "schema_summary": state["schema"],
        "data_result_summary": _data_result_summary(
            state["request"].get("rows") or []
        ),
        "validator_results": state["validator_results"],
        "sql_hash": state["sql_hash"],
    }
    coverage = _invoke_llm(state, "data_coverage_interpretation", coverage_payload)
    block_reason = _local_coverage_block_reason(state)
    answerable_reason = _local_coverage_answerable_reason(state)
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


def _execute_capabilities(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "execute_capabilities")
    rows = _capability_rows(state)
    query_ref = _capability_result_refs(state)
    evidence = []
    compiled = state["compiled_graph"]
    capabilities = tuple(compiled.mutations.accepted_graph)
    budget = state.get("budget_state") or default_budget("ordinary")

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
                    role=state["request"].get("role", "analyst"),
                    budget_state=budget,
                    llm_business_reason="检查本次聚合结果是否足以支撑业务判断。",
                    params={
                        "rows": capability_rows,
                        "result_refs": capability_refs,
                        "required_fields": tuple(
                            state["request"].get("required_fields", ())
                        ),
                    },
                )
            )
        )
        budget = record_capability_call(budget)

    for capability_id in (
        capability
        for capability in capabilities
        if capability in PATTERN_COMPARE_CAPABILITIES
    ):
        capability_rows = _capability_rows_for(state, capability_id)
        capability_refs = _capability_result_refs_for(state, capability_id)
        pattern_family = state["intent"]["pattern_family"]
        pattern_params = dict(state["intent"].get("pattern_params", {}))
        if pattern_family == "intra_period":
            pattern_params.setdefault("target_phase", "start")
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
                    claim_type=state["intent"].get("target_claim", ""),
                    metric=state["intent"]["target_metric"],
                    scope=state["intent"]["scope"],
                    time_window=state["intent"]["time_window"],
                    baseline=baseline,
                    target=target,
                    grain=pattern_family,
                    filters={},
                    dimensions=(),
                    contract_versions={},
                    role=state["request"].get("role", "analyst"),
                    budget_state=budget,
                    llm_business_reason="执行已接受的业务对比能力。",
                    params={
                        "rows": capability_rows,
                        "result_refs": capability_refs,
                        "pattern_family": pattern_family,
                        **pattern_params,
                    },
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
        capability_rows = _capability_rows_for(state, "pattern_scan")
        capability_refs = _capability_result_refs_for(state, "pattern_scan")
        pattern_family = state["intent"]["pattern_family"]
        pattern_params = dict(state["intent"].get("pattern_params", {}))
        if pattern_family == "intra_period":
            pattern_params.setdefault("target_phase", "start")
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
        evidence.append(
            formula_decompose(
                [{"formula_id": "paid_amount", "components": ("paid_amount",)}],
                available_components=("paid_amount",),
                result_refs=_capability_result_refs_for(state, "formula_decompose"),
            )
        )
    if "driver_decomposition" in capabilities:
        capability_rows = _capability_rows_for(state, "driver_decomposition")
        capability_refs = _capability_result_refs_for(state, "driver_decomposition")
        evidence.append(
            driver_decomposition(
                capability_rows,
                result_refs=capability_refs,
                **_driver_params(state),
            )
        )
    if "segment_contribution" in capabilities:
        capability_rows = _capability_rows_for(state, "segment_contribution")
        capability_refs = _capability_result_refs_for(state, "segment_contribution")
        evidence.append(
            segment_contribution(
                capability_rows,
                result_refs=capability_refs,
                **_segment_contribution_params(state),
            )
        )
    if "outlier_contribution" in capabilities:
        capability_rows = _capability_rows_for(state, "outlier_contribution")
        capability_refs = _capability_result_refs_for(state, "outlier_contribution")
        evidence.append(
            outlier_contribution(
                capability_rows,
                result_refs=capability_refs,
                **_outlier_contribution_params(state),
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
            event_evidence(
                state["request"].get("events", ()),
                result_refs=_capability_result_refs_for(state, "event_evidence"),
                **_event_evidence_params(state),
            )
        )
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
        evidence.append(
            outlier_scan(
                _capability_rows_for(state, "outlier_scan"),
                result_refs=_capability_result_refs_for(state, "outlier_scan"),
            )
        )
    if "joint_attribution" in capabilities:
        capability_rows = _capability_rows_for(state, "joint_attribution")
        capability_refs = _capability_result_refs_for(state, "joint_attribution")
        evidence.append(
            joint_attribution(
                capability_rows,
                segment_evidence=segment,
                result_refs=capability_refs,
                **_joint_attribution_params(state),
            )
        )

    state["evidence"] = [_evidence_dict(item, state) for item in evidence]
    return state


def _reduce_evidence(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "reduce_evidence")
    pattern = _pattern_evidence(state)
    primary = pattern or _primary_business_evidence(state)
    pattern_ref = pattern.get(
        "evidence_ref",
        primary.get("evidence_ref", f"pattern_scan:{state['intent']['pattern_family']}"),
    )
    state["evidence_brief"] = {
        "pattern_ref": pattern_ref,
        "pattern_status": primary.get("strength", "insufficient"),
        "pattern_established": _evidence_established(primary),
        "wording_limit": primary.get("wording_limit", "unknown"),
        "primary_capability": primary.get("capability_id") or primary.get("capability"),
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


def _capability_rows(state: WorkflowState) -> Sequence[Mapping[str, Any]]:
    return state.get("request", {}).get("rows") or state.get("rows", ()) or _default_pattern_rows()


def _capability_result_refs(state: WorkflowState) -> tuple[str, ...]:
    return tuple(state.get("request", {}).get("result_refs") or (state.get("sql_hash", ""),))


def _capability_rows_for(
    state: WorkflowState,
    capability_id: str,
) -> Sequence[Mapping[str, Any]]:
    rows_by_intent = state.get("request", {}).get("runtime_rows_by_intent") or {}
    if isinstance(rows_by_intent, Mapping):
        for intent in _capability_query_intents(capability_id):
            if intent in rows_by_intent:
                rows = rows_by_intent[intent]
                if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                    return rows
    return _capability_rows(state)


def _capability_result_refs_for(state: WorkflowState, capability_id: str) -> tuple[str, ...]:
    refs_by_intent = state.get("request", {}).get("result_refs_by_intent") or {}
    if isinstance(refs_by_intent, Mapping):
        for intent in _capability_query_intents(capability_id):
            if intent in refs_by_intent:
                refs = refs_by_intent[intent]
                if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)):
                    return tuple(str(ref) for ref in refs if ref)
    return _capability_result_refs(state)


def _capability_query_intents(capability_id: str) -> tuple[str, ...]:
    if capability_id in {"data_quality_profile", "data_quality_check"}:
        return ("data_quality_probe", "daily_metric_baselines", "clickhouse_revenue_rows")
    if capability_id in {"segment_contribution", "segment_bridge", "user_mix_contribution", "high_value_user_contribution"}:
        return ("dimension_scan", "joint_candidate_scan", "daily_metric_baselines", "clickhouse_revenue_rows")
    if capability_id == "joint_attribution":
        return ("joint_candidate_scan", "dimension_scan", "daily_metric_baselines", "clickhouse_revenue_rows")
    if capability_id == "event_evidence":
        return ("event_context_probe", "daily_metric_baselines", "clickhouse_revenue_rows")
    if capability_id in {
        "compare_periods",
        "rolling_window_compare",
        "driver_decomposition",
        "outlier_scan",
        "outlier_contribution",
        "pattern_scan",
    }:
        return ("daily_metric_baselines", "dimension_scan", "joint_candidate_scan", "clickhouse_revenue_rows")
    return ("clickhouse_revenue_rows", "daily_metric_baselines", "dimension_scan", "joint_candidate_scan")


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
            if isinstance(dimensions, Sequence) and not isinstance(dimensions, (str, bytes)):
                keys = tuple(str(key) for key in dimensions if key)
                if keys:
                    return keys
    return ()


def _driver_params(state: WorkflowState) -> dict[str, Any]:
    params = dict(state.get("intent", {}).get("pattern_params", {}))
    return {
        "period_key": params.get("period_key", "period"),
        "group_key": params.get("group_key", "group"),
        "target_group": params.get("target_group", "target"),
        "baseline_group": params.get("baseline_group", "baseline"),
    }


def _segment_contribution_params(state: WorkflowState) -> dict[str, Any]:
    params = dict(state.get("intent", {}).get("pattern_params", {}))
    runtime_dimensions = _runtime_dimension_keys_for_intents(
        state,
        ("dimension_scan", "joint_candidate_scan"),
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
        or ()
    )
    if isinstance(dimensions, str):
        dimensions = tuple(part.strip() for part in dimensions.split(",") if part.strip())
    return {
        "dimension_keys": tuple(dimensions),
        "group_key": params.get("group_key", "group"),
        "target_group": params.get("target_group", "target"),
        "baseline_group": params.get("baseline_group", "baseline"),
        "amount_key": params.get("amount_key", "amount"),
        "force_run": has_explicit_dimensions,
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
    requested = _requested_node_ids(
        state.get("analysis_route", {}).get("promotion", {}).get("requested_nodes", ())
    )
    if "joint_attribution" in requested:
        _current_event(state)["route"] = "accepted"
    else:
        _current_event(state)["route"] = "degraded_or_skip"
    return state


def _route_capability_cards() -> list[dict[str, Any]]:
    return [
        card
        for card in llm_capability_cards()
        if card.get("capability_id") not in ROUTE_BLOCKED_CAPABILITY_IDS
    ]


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
        for key in ("pattern_family", "pattern_params", "time_window", "baseline", "target", "scope")
        if intent.get(key) not in ("", None, {}, [])
    }
    if request.get("role"):
        context["permission_scope"] = str(request.get("role"))
    manifest = request.get("context_manifest")
    if isinstance(manifest, Mapping):
        snapshot_version = manifest.get("snapshot_version")
        if snapshot_version not in ("", None):
            context["snapshot_version"] = str(snapshot_version)
        permission_context = manifest.get("permission_context")
        if (
            "permission_scope" not in context
            and isinstance(permission_context, Mapping)
            and permission_context.get("role") not in ("", None)
        ):
            context["permission_scope"] = str(permission_context.get("role"))
    if isinstance(request.get("runtime_windows"), dict):
        context["windows"] = dict(request["runtime_windows"])
    elif isinstance(analysis_route.get("windows"), dict):
        context["windows"] = dict(analysis_route["windows"])
    if request.get("runtime_baselines"):
        context["baselines"] = tuple(request["runtime_baselines"])
    elif analysis_route.get("baselines"):
        context["baselines"] = tuple(analysis_route["baselines"])
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
    if label:
        return str(label)
    return _target_label_from_pattern_params(dict(intent.get("pattern_params", {})))


def _baseline_label(state: WorkflowState) -> str:
    intent = state.get("intent", {})
    label = intent.get("baseline", {}).get("label")
    if label:
        return str(label)
    return _baseline_label_from_pattern_params(dict(intent.get("pattern_params", {})))


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
                _capability_rows(state),
                segment_evidence=segment,
                result_refs=_capability_result_refs(state),
                **_joint_attribution_params(state),
            ),
            state,
        )
    )
    state["evidence"] = evidence
    return state


def _interpret_evidence(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "interpret_evidence")
    evidence_payload = {
        "intent": state["intent"],
        "evidence_brief": state["evidence_brief"],
        "evidence": state["evidence"],
    }
    output = _invoke_llm(state, "evidence_interpretation", evidence_payload)
    state["evidence_interpretation"] = _normalize_evidence_interpretation_output(
        output,
        state,
    )
    return state


def _audit_causal_implications(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "audit_causal_implications")
    dossier = _build_causal_evidence_dossier(state)
    state["causal_evidence_dossier"] = dossier
    state["causal_audit"] = _invoke_llm(
        state,
        "causal_audit",
        {
            "intent": state.get("intent", {}),
            "evidence_brief": state.get("evidence_brief", {}),
            "evidence": state.get("evidence", []),
            "evidence_interpretation": state.get("evidence_interpretation", {}),
            "causal_evidence_dossier": dossier,
        },
    )
    return state


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
    return {
        key: _normalize_evidence_interpretation_text(value, state)
        if isinstance(value, str)
        else value
        for key, value in output.items()
    }


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
    answer_payload = {
        "intent": state["intent"],
        "evidence_interpretation": state["evidence_interpretation"],
        "evidence_brief": state["evidence_brief"],
        "evidence": state["evidence"],
        "evidence_refs": [item.get("evidence_ref") for item in state["evidence"]],
        "answer_context": _answer_synthesis_context(state),
    }
    output = _invoke_llm(state, "answer_synthesis", answer_payload)
    state["answer_text"] = _weaken_unsupported_causal_wording(output.get("answer_text", ""))
    requested_claims = state["request"].get("draft_claims")
    state["draft_claims"] = _claims_from_llm_or_default(
        requested_claims if requested_claims is not None else output.get("claims"),
        state,
    )
    _ensure_business_narrative_answer(state)
    return state


def _semantic_audit(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "semantic_audit")
    audit_payload = {
        "answer_text": state.get("answer_text", ""),
        "draft_claims": state["draft_claims"],
        "evidence": state.get("evidence", []),
        "evidence_brief": state["evidence_brief"],
        "evidence_refs": [item.get("evidence_ref") for item in state.get("evidence", [])],
        "answer_context": _answer_synthesis_context(state),
        "wording_boundary": "causal and main-driver wording require explicit supporting evidence",
    }
    state["semantic_audit"] = _invoke_llm(state, "semantic_audit", audit_payload)
    audit = state["semantic_audit"]
    status = str(audit.get("audit_status", "")).lower()
    if status in {"fail", "failed", "needs_revision"} or audit.get("issues"):
        state["retry_context"] = _retry_context(
            "semantic_audit",
            "semantic_audit",
            audit.get("issues", []) or audit.get("audit_status", ""),
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
    output = _invoke_llm(
        state,
        "answer_repair",
        {
            "answer_text": state.get("answer_text", ""),
            "draft_claims": state["draft_claims"],
            "semantic_audit": state["semantic_audit"],
            "verifier": state.get("verifier", {}),
            "retry_context": state.get("retry_context", {}),
            "evidence_brief": state["evidence_brief"],
            "answer_context": _answer_synthesis_context(state),
        },
    )
    state["answer_text"] = _weaken_unsupported_causal_wording(
        output.get("answer_text", state.get("answer_text", ""))
    )
    state["draft_claims"] = _claims_from_llm_or_default(output.get("claims"), state)
    _ensure_business_narrative_answer(state)
    return state


def _generate_degraded_explanation(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "generate_degraded_explanation")
    contract_gap_diagnostics = _refresh_contract_gap_diagnostics(state)
    explanation_payload = {
        "intent": state.get("intent", {}),
        "evidence_brief": state.get("evidence_brief", {}),
        "verifier": state.get("verifier", {}),
        "contract_gap_diagnostics": contract_gap_diagnostics,
    }
    output = _invoke_llm(state, "degraded_explanation", explanation_payload)
    state["final_explanation"] = _sanitize_terminal_explanation(output, state, "degraded")
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

    draft_claims = list(state.get("draft_claims") or [])
    if draft_claims:
        state["draft_claims"] = draft_claims
        return
    evidence = _primary_business_evidence(state)
    evidence_ref = str(evidence.get("evidence_ref") or evidence_items[0].get("evidence_ref"))
    state["draft_claims"] = [_degraded_boundary_claim(state, evidence_ref)]


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


def _degraded_boundary_claim(state: WorkflowState, evidence_ref: str) -> dict[str, Any]:
    reason = "、".join(_business_limitation_reasons(tuple(_degraded_limitations(state))))
    if not reason:
        reason = "当前证据强度不足"
    return _with_claim_audit(
        state,
        {
            "text": f"当前证据不足以支撑主业务结论；主要限制是{reason}。",
            "evidence_refs": [evidence_ref],
            "numbers": {},
            "scope": state["intent"]["scope"],
            "time_window": state["intent"]["time_window"],
            "claim_strength": "insufficient",
        },
    )


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
        explanation_payload = {
            "intent": state.get("intent", {}),
            "boundary_decision": state.get("boundary_decision", {}),
            "validator_results": state.get("validator_results", []),
            "contract_gap_diagnostics": contract_gap_diagnostics,
        }
        output = _invoke_llm(state, "blocked_explanation", explanation_payload)
        state["final_explanation"] = _sanitize_terminal_explanation(output, state, "blocked")
        state["verifier"] = {"status": "terminal_explanation", "errors": [], "warnings": []}
    if "evidence" not in state:
        state["evidence"] = []
    if "draft_claims" not in state:
        state["draft_claims"] = []
    _ensure_blocked_boundary_audit(state)
    return state


def _ensure_blocked_boundary_audit(state: WorkflowState) -> None:
    for evidence, claim_builder in (
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

    draft_claims = list(state.get("draft_claims") or [])
    if not any(evidence_ref in claim.get("evidence_refs", ()) for claim in draft_claims):
        draft_claims.append(claim_builder(state, evidence_ref))
    state["draft_claims"] = draft_claims


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
            "permission_blocked",
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
    if status == "permission_blocked":
        return "依赖的聚合信息受权限限制，本轮不能发布对应业务结论"
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
        output = _invoke_llm(state, "final_business_summary", summary_payload)
    except WorkflowFailure as exc:
        if not _is_timeout_failure(exc) or not state.get("answer_text"):
            raise
        state["final_business_summary"] = _normalize_visible_business_text(
            _weaken_unsupported_causal_wording(state["answer_text"]),
            state,
        )
        state["final_summary_display_warnings"] = sorted(
            {*state.get("final_summary_display_warnings", []), "final_summary_timeout"}
        )
        return state
    _apply_final_business_summary_output(state, output)
    return state


def _answer_quality_gate(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "answer_quality_gate")
    state["follow_up_questions"] = _follow_up_questions(state)
    legacy_quality_gate = evaluate_answer_quality(
        user_question=str(state.get("request", {}).get("question") or ""),
        verified_claims=_verified_claims(state),
        final_answer=state.get("final_business_summary") or state.get("answer_text", ""),
        follow_up_questions=state["follow_up_questions"],
    )
    if state.get("final_summary_display_warnings"):
        legacy_quality_gate = {
            **legacy_quality_gate,
            "final_summary_display_warnings": state["final_summary_display_warnings"],
        }
    if state.get("final_explanation") and not state.get("draft_claims"):
        legacy_quality_gate = {
            **legacy_quality_gate,
            "has_verified_claims": False,
            "verified_claim_preserved": True,
            "business_insight_present": True,
            "issues": [
                issue
                for issue in legacy_quality_gate.get("issues", [])
                if issue not in {"missing_verified_claim", "missing_business_insight"}
            ],
        }

    final_answer_audit = _final_answer_audit(state)
    if (
        final_answer_audit["retry_instruction"]
        and not final_answer_audit["blocks_display"]
        and final_answer_audit["repairable_warnings"]
    ):
        try:
            output = _invoke_llm(
                state,
                "final_business_summary",
                _final_business_summary_payload(
                    state,
                    retry_instruction=final_answer_audit["retry_instruction"],
                ),
            )
        except WorkflowFailure:
            pass
        else:
            _apply_final_business_summary_output(state, output)
            legacy_quality_gate = evaluate_answer_quality(
                user_question=str(state.get("request", {}).get("question") or ""),
                verified_claims=_verified_claims(state),
                final_answer=state.get("final_business_summary") or state.get("answer_text", ""),
                follow_up_questions=state["follow_up_questions"],
            )
            if state.get("final_summary_display_warnings"):
                legacy_quality_gate = {
                    **legacy_quality_gate,
                    "final_summary_display_warnings": state["final_summary_display_warnings"],
                }
            if state.get("final_explanation") and not state.get("draft_claims"):
                legacy_quality_gate = {
                    **legacy_quality_gate,
                    "has_verified_claims": False,
                    "verified_claim_preserved": True,
                    "business_insight_present": True,
                    "issues": [
                        issue
                        for issue in legacy_quality_gate.get("issues", [])
                        if issue not in {"missing_verified_claim", "missing_business_insight"}
                    ],
                }
            final_answer_audit = _final_answer_audit(state)

    state["final_answer_audit"] = final_answer_audit
    state["quality_gate"] = {
        **legacy_quality_gate,
        "display_status": final_answer_audit["display_status"],
        "hard_blockers": list(final_answer_audit["hard_blockers"]),
        "repairable_warnings": list(final_answer_audit["repairable_warnings"]),
        "retry_instruction": final_answer_audit["retry_instruction"],
        "business_audit_summary": final_answer_audit["business_audit_summary"],
        "issues": [
            *list(legacy_quality_gate.get("issues", [])),
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
    if "rows" in request:
        return list(request.get("rows") or [])
    return _default_pattern_rows()


def _coverage_text_requests_confirmation(coverage: Mapping[str, Any]) -> bool:
    text = " ".join(str(coverage.get(key) or "") for key in ("business_impact", "decision_summary"))
    return any(token in text for token in ("确认", "补充", "调整查询", "无法直接", "不能直接", "不可直接"))


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
        if _evidence_supports_bounded_answer(state):
            _current_event(state)["route"] = "semantic_sanitized_to_bounded_answer"
            return "sanitize"
        return "degrade"
    return "verify"


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


def _build_answer_package_from_state(state: WorkflowState) -> dict[str, Any]:
    compiled = state.get("compiled_graph")
    proposed_graph = compiled.mutations.proposed_graph if compiled else ()
    accepted_graph = compiled.mutations.accepted_graph if compiled else ()
    records = compiled.mutations.records if compiled else ()
    request = state.get("request", {})
    context_manifest = request.get("context_manifest") or {}
    contract_gap_diagnostics = state.get("contract_gap_diagnostics")
    if contract_gap_diagnostics is None:
        contract_gap_diagnostics = _contract_gap_diagnostics_from_state(state)
        state["contract_gap_diagnostics"] = contract_gap_diagnostics
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
        final_business_summary=state.get("final_business_summary", ""),
        coverage_interpretation=state.get("coverage_interpretation", {}),
        clarification_outcome=state.get("clarification_outcome", {}),
        causal_audit=state.get("causal_audit", {}),
        causal_evidence_dossier=state.get("causal_evidence_dossier", {}),
        context_manifest_ref=str(context_manifest.get("manifest_id") or ""),
        reuse_decisions=request.get("reuse_decisions", ()),
        quality_gate=state.get("quality_gate", {}),
        follow_up_questions=state.get("follow_up_questions", ()),
        compiler_runtime_plan=request.get("compiler_runtime_plan", {}),
        contract_gap_diagnostics=contract_gap_diagnostics,
        row_query_plan=state.get("row_query_plan", {}),
        snapshot_id=str(context_manifest.get("snapshot_version") or request.get("snapshot_id") or ""),
        permission_scope=str(request.get("role") or ""),
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
    return diagnose_contract_gaps(
        contract_gaps=tuple(contract_gaps),
        available_fields=available_fields,
        contract_fields=contract_fields,
        permission_denied_fields=request.get("permission_denied_fields", ()),
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
        for marker in ("当前证据能把排查方向收敛到", "排查方向", "下一步最值得", "洞察")
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
    numbers = claim.get("numbers")
    if not isinstance(numbers, Mapping) or not numbers:
        return False
    return all(_number_value_present(value, answer) for value in numbers.values())


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


def repair_final_answer_with_verified_claim(
    state: WorkflowState,
    quality_gate: Mapping[str, Any],
) -> str:
    answer = str(state.get("final_business_summary") or state.get("answer_text") or "").strip()
    claims = _verified_claims(state)
    claim_text = str(claims[0].get("text") or "").strip() if claims else ""
    if claim_text:
        conclusion = (
            f"最终结论：已验证结论是：{claim_text} "
            f"当前证据能把排查方向收敛到{_quality_gate_focus(state)}；"
            "还不能直接说这是唯一原因或已被因果证明。"
        )
    else:
        conclusion = (
            f"最终结论：当前证据能把排查方向收敛到{_quality_gate_focus(state)}；"
            "还不能直接说这是唯一原因或已被因果证明。"
        )
    if "最终结论：" in answer:
        return re.sub(r"最终结论：.*?(?=\n需要注意：|$)", conclusion, answer, count=1, flags=re.S)
    if answer:
        return f"{answer}\n{conclusion}"
    return "\n".join(
        (
            _question_understanding_sentence(state),
            _analysis_path_sentence(state).replace("分析思路：", "分析脉络：", 1),
            _key_findings_sentence(state),
            conclusion,
            _attention_sentence(state),
        )
    )


def _verified_claims(state: WorkflowState) -> list[dict[str, Any]]:
    if state.get("verifier", {}).get("errors"):
        return []
    return [dict(claim) for claim in state.get("draft_claims", []) if isinstance(claim, Mapping)]


def _quality_gate_focus(state: WorkflowState) -> str:
    labels = _capability_path_labels(
        tuple(
            state.get("compiled_graph").mutations.accepted_graph
            if state.get("compiled_graph")
            else ()
        )
    )
    return labels or "已验证的变化方向、贡献项和仍需验证的候选解释"


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
    allowed_hard_blockers = {
        "permission_leak",
        "sql_security_failure",
        "unsupported_main_claim",
        "verifier_evidence_contradiction",
    }
    allowed_warnings = {
        "claim_paraphrase_drift",
        "claim_paraphrase_unclear",
        "missing_business_interpretation",
        "weak_business_interpretation",
        "weak_followup",
        "missing_wording_anchor",
        "missing_required_summary_markers",
        "internal_visible_token",
        "unsupported_wording",
        "missing_pattern_evidence",
        "missing_driver_claim",
        "missing_primary_claim",
    }
    status = str(output.get("display_status") or "ready_with_warnings")
    if status not in {"ready", "ready_with_warnings", "hard_blocked"}:
        status = "ready_with_warnings"
    hard_blockers = [
        code
        for code in dict.fromkeys(str(item) for item in output.get("hard_blockers") or ())
        if code in allowed_hard_blockers
    ]
    warnings = [
        code
        for code in dict.fromkeys(str(item) for item in output.get("repairable_warnings") or ())
        if code in allowed_warnings
    ]
    audit = {
        "display_status": status,
        "blocks_display": bool(hard_blockers),
        "hard_blockers": hard_blockers,
        "repairable_warnings": warnings,
        "retry_instruction": str(output.get("retry_instruction") or ""),
        "business_audit_summary": str(output.get("business_audit_summary") or ""),
    }
    if audit["display_status"] == "hard_blocked" and not audit["hard_blockers"]:
        audit["display_status"] = "ready_with_warnings" if audit["repairable_warnings"] else "ready"
    if audit["display_status"] == "ready_with_warnings" and not audit["repairable_warnings"]:
        audit["display_status"] = "ready"
    if not audit["blocks_display"] and audit["repairable_warnings"] and audit["display_status"] == "ready":
        audit["display_status"] = "ready_with_warnings"
    return audit


def _final_business_summary_payload(
    state: WorkflowState,
    *,
    retry_instruction: str = "",
) -> dict[str, Any]:
    contract_gap_diagnostics = _refresh_contract_gap_diagnostics(state)
    return {
        "intent": state.get("intent", {}),
        "confirmed_understanding": state.get("confirmed_understanding", {}),
        "accepted_graph": to_jsonable(
            state.get("compiled_graph").mutations.accepted_graph
            if state.get("compiled_graph")
            else ()
        ),
        "evidence_brief": state.get("evidence_brief", {}),
        "evidence_interpretation": state.get("evidence_interpretation", {}),
        "answer_text": state.get("answer_text", ""),
        "claims": state.get("draft_claims", []),
        "semantic_audit": state.get("semantic_audit", {}),
        "verifier": state.get("verifier", {}),
        "final_explanation": state.get("final_explanation", {}),
        "contract_gap_diagnostics": contract_gap_diagnostics,
        "checkpoint_summary": _checkpoint_summary(state),
        "business_threads": _business_threads(state),
        "final_answer_retry_instruction": retry_instruction,
    }


def _apply_final_business_summary_output(
    state: WorkflowState,
    output: Mapping[str, Any],
) -> None:
    state["final_business_summary"] = _weaken_unsupported_causal_wording(
        output.get("summary_text", "")
    )
    state["final_business_summary"] = _normalize_visible_business_text(
        state["final_business_summary"],
        state,
    )
    state["final_summary_display_warnings"] = _final_summary_display_repair_reasons(
        state["final_business_summary"],
        state,
    )


def _final_answer_audit(state: WorkflowState) -> dict[str, Any]:
    audit = normalize_final_answer_audit(
        _invoke_llm(
            state,
            "final_answer_audit",
            {
                "user_question": state.get("request", {}).get("question", ""),
                "verified_claims": _verified_claims(state),
                "final_answer": state.get("final_business_summary") or state.get("answer_text", ""),
                "follow_up_questions": state.get("follow_up_questions", ()),
                "compiler_runtime_plan": state.get("request", {}).get(
                    "compiler_runtime_plan", {}
                ),
                "verifier": state.get("verifier", {}),
                "semantic_audit": state.get("semantic_audit", {}),
                "final_summary_display_warnings": state.get(
                    "final_summary_display_warnings", ()
                ),
                "evidence_brief": state.get("evidence_brief", {}),
            },
        )
    )
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


def _local_final_answer_hard_blockers(state: WorkflowState) -> list[str]:
    blockers: list[str] = []
    validators = state.get("validator_results", ())
    if any(
        isinstance(item, Mapping)
        and item.get("validator") == "permission"
        and not item.get("ok", False)
        for item in validators
    ):
        blockers.append("permission_leak")
    if any(
        isinstance(item, Mapping)
        and item.get("validator") == "sql_safety"
        and not item.get("ok", False)
        for item in validators
    ):
        blockers.append("sql_security_failure")
    verifier_errors = state.get("verifier", {}).get("errors") or ()
    if verifier_errors:
        blockers.append("verifier_evidence_contradiction")
        unsupported_main_claim_codes = {
            "missing_evidence_ref",
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


def _invoke_llm(state: WorkflowState, task: str, payload: dict[str, Any]) -> dict[str, Any]:
    spec = build_prompt(task, payload)
    try:
        result = state["llm_client"].invoke_json(
            task=spec.task,
            prompt_version=spec.prompt_version,
            messages=spec.messages,
            required_keys=spec.required_keys,
        )
    except Exception as exc:
        raise WorkflowFailure(str(exc), failure_type="llm") from exc
    state["llm_calls"].append(result.audit)
    return result.output


def _local_llm_fallback_audit(
    *,
    task: str,
    payload: dict[str, Any],
    output: dict[str, Any],
    exc: Exception,
) -> dict[str, Any]:
    spec = build_prompt(task, payload)
    now = _utc_now()
    return {
        "task": spec.task,
        "provider": "local_fallback",
        "model": "deterministic_capability_router",
        "prompt_version": spec.prompt_version,
        "response_id": f"local-fallback-{task}",
        "messages": [dict(message) for message in spec.messages],
        "required_keys": list(spec.required_keys),
        "raw_response_content": json.dumps(output, ensure_ascii=False, sort_keys=True),
        "started_at": now,
        "finished_at": now,
        "duration_ms": 0.0,
        "failure_type": "llm_unavailable",
        "error_class": exc.__class__.__name__,
        "error": str(exc),
        "usage": {},
        "structured_output": output,
    }


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
    return evidence


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


def _sanitize_to_bounded_pattern_answer(state: WorkflowState) -> None:
    claim = _default_claim_from_evidence(state)
    state["draft_claims"] = [claim]
    state["answer_text"] = _business_narrative_answer(state, claim)


def _single_period_pattern(state: WorkflowState) -> bool:
    pattern = _pattern_evidence(state)
    try:
        return int(pattern.get("typed_payload", {}).get("comparable_periods", 0)) <= 1
    except (TypeError, ValueError):
        return False


def _claims_from_llm_or_default(claims: Any, state: WorkflowState) -> list[dict[str, Any]]:
    evidence_by_ref = _evidence_by_ref(state.get("evidence", []))
    evidence_refs = set(evidence_by_ref)
    normalized = []
    seen = set()
    for claim in claims or ():
        if not isinstance(claim, Mapping):
            continue
        refs = [
            ref for ref in claim.get("evidence_refs", ()) if ref in evidence_refs
        ]
        refs = _prioritize_claim_refs(refs, evidence_by_ref)
        text = _weaken_unsupported_causal_wording(
            claim.get("text") or claim.get("claim_text") or claim.get("claim")
        )
        if not refs or not text:
            continue
        dedupe_key = (text, tuple(refs))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(
            _with_claim_audit(
                state,
                {
                    "text": str(text),
                    "evidence_refs": refs,
                    "numbers": _normalize_claim_numbers(
                        claim.get("numbers") or claim.get("numeric_facts") or {},
                        refs,
                        evidence_by_ref,
                    ),
                    "scope": claim.get("scope", state["intent"]["scope"]),
                    "time_window": claim.get("time_window", state["intent"]["time_window"]),
                    "claim_strength": claim.get("claim_strength"),
                },
            )
        )
    return normalized or [_default_claim_from_evidence(state)]


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
    key_map = {
        "付费金额提升比例": "amount_delta_ratio",
        "付费金额变化比例": "amount_delta_ratio",
        "单付费用户金额贡献占比": "unit_value_share",
        "单均金额贡献占比": "unit_value_share",
        "单位价值贡献占比": "unit_value_share",
        "付费用户数贡献占比": "volume_share",
        "用户数贡献占比": "volume_share",
        "订单数贡献占比": "volume_share",
    }
    normalized = {}
    available = set()
    for ref in refs:
        available.update((evidence_by_ref.get(ref, {}).get("typed_payload") or {}).keys())
    for raw_key, raw_value in numbers.items():
        key = key_map.get(str(raw_key), raw_key)
        value = _percentage_to_ratio(raw_value) if key in key_map.values() else raw_value
        if not available or key in available:
            normalized[str(key)] = value
    return normalized


def _percentage_to_ratio(value: Any) -> Any:
    numeric = _as_float(value)
    if numeric is None:
        return value
    if abs(numeric) > 1 and abs(numeric) <= 100:
        return numeric / 100
    return numeric


def _ensure_business_narrative_answer(state: WorkflowState) -> None:
    claims = state.get("draft_claims") or []
    if not claims:
        return
    answer_text = state.get("answer_text", "")
    if _answer_needs_business_narrative(answer_text) or (
        _single_period_pattern(state) and _answer_has_single_period_overclaim(answer_text)
    ):
        state["answer_text"] = _business_narrative_answer(state, claims[0])


def _answer_needs_business_narrative(text: Any) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    required_marker_groups = (
        ("理解",),
        ("分析思路", "分析路径", "怎么分析", "证据路径"),
        ("关键发现", "发现"),
        ("结论",),
        ("需要注意", "注意", "继续观察", "可观察"),
    )
    return any(not any(marker in value for marker in group) for group in required_marker_groups)


def _answer_has_single_period_overclaim(text: Any) -> bool:
    value = str(text or "").lower()
    markers = (
        "high-confidence",
        "non-random",
        "statistically significant",
        "统计显著",
        "显著规律",
        "可靠规律",
        "稳定规律",
        "非随机",
    )
    return any(marker in value for marker in markers)


def _answer_synthesis_context(state: WorkflowState) -> dict[str, Any]:
    claim = _default_claim_from_evidence(state)
    pattern = _pattern_evidence(state)
    primary = pattern or _primary_business_evidence(state)
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
            "typed_payload": payload,
        },
        "evidence_boundary": _attention_sentence(state),
        "capability_business_findings": _capability_business_findings(state),
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


def _business_narrative_answer(state: WorkflowState, claim: dict[str, Any]) -> str:
    return "\n".join(
        (
            _question_understanding_sentence(state),
            _analysis_path_sentence(state),
            _key_findings_sentence(state),
            _conclusion_sentence(state, claim),
            _attention_sentence(state),
        )
    )


def _final_summary_needs_display_repair(text: Any, state: WorkflowState) -> bool:
    return bool(_final_summary_display_repair_reasons(text, state))


def _final_summary_display_repair_reasons(text: Any, state: WorkflowState) -> list[str]:
    value = str(text or "")
    markers = ("我对问题的理解", "分析脉络", "关键发现", "最终结论", "需要注意")
    reasons = []
    if any(marker not in value for marker in markers):
        reasons.append("missing_required_summary_markers")
    if _has_internal_visible_token(value):
        reasons.append("internal_visible_token")
    if _final_summary_has_unsupported_wording(value, state):
        reasons.append("unsupported_wording")
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


def _final_summary_has_unsupported_wording(text: str, state: WorkflowState) -> bool:
    if any(
        token in text
        for token in (
            "语义审计",
            "硬验证",
            "硬校验",
            "verifier",
            "方向命中率",
            "重要性命中率",
            "单用户/单订单",
        )
    ):
        return True
    if _has_materiality_data_volume_drift(text, state):
        return True
    if _has_materiality_ratio_threshold_drift(text):
        return True
    if _has_unsupported_comparable_period_drift(text, state):
        return True
    if _final_summary_missing_limitation_reason(text, state):
        return True
    if _repair_path_invents_fixed_future_window(text):
        return True
    if any(token in text for token in ("全年或多年", "合同条款")):
        return True
    if _single_period_pattern(state) and any(
        token in text for token in ("证据充分", "充分支持", "可靠结论")
    ):
        return True
    if _single_period_pattern(state) and SINGLE_PERIOD_CONFIDENCE.search(text):
        return True
    if _has_positive_causal_wording(text):
        evidence_by_ref = _evidence_by_ref(state.get("evidence", []))
        refs = tuple((state.get("draft_claims") or [{}])[0].get("evidence_refs", ()))
        return not any(
            evidence_by_ref.get(ref, {}).get("evidence_type") == "causal_evidence"
            for ref in refs
        )
    return False


def _final_summary_missing_limitation_reason(text: str, state: WorkflowState) -> bool:
    limitations = tuple(state.get("evidence_brief", {}).get("limitations", ()))
    if not limitations:
        return False
    if "insufficient_comparable_periods" in limitations and "可比周期" not in text:
        return True
    if "no_comparable_periods" in limitations and "可比周期" not in text:
        return True
    if "weak_direction" in limitations and "方向" not in text:
        return True
    if "below_materiality_floor" in limitations and not any(
        token in text for token in ("重要性阈值", "变化幅度")
    ):
        return True
    return False


def _has_positive_causal_wording(text: str) -> bool:
    for sentence in re.split(r"[。；;.!?？\n]+", text):
        if not CAUSAL_WORDING.search(sentence):
            continue
        if any(
            marker in sentence
            for marker in ("不能", "无法", "不可", "不支持", "缺乏", "没有", "暂不")
        ):
            continue
        if any(marker in sentence for marker in ("是否", "是不是", "有没有", "能否", "能不能")):
            continue
        return True
    return False


def _final_business_summary_fallback(state: WorkflowState) -> str:
    claims = state.get("draft_claims") or []
    if claims:
        claim = _preferred_final_claim(claims)
        return "\n".join(
            (
                _question_understanding_sentence(state),
                _analysis_path_sentence(state).replace("分析思路：", "分析脉络：", 1),
                _key_findings_sentence(state),
                _conclusion_sentence(state, claim).replace("结论：", "最终结论：", 1),
                _attention_sentence(state),
            )
        )

    final = state.get("final_explanation", {})
    explanation = _businessize_internal_tokens(str(final.get("explanation") or "当前证据不足。"))
    repair_path = _businessize_internal_tokens(str(final.get("repair_path") or "补充证据后重跑。"))
    key_findings = (
        _key_findings_sentence(state)
        if _pattern_evidence(state)
        else f"关键发现：{explanation}"
    )
    return "\n".join(
        (
            _question_understanding_sentence(state),
            "分析脉络：我先确认问题边界、数据口径和可执行分析路径，再检查当前证据是否足以支持这个结论。",
            key_findings,
            "最终结论：当前证据不足以发布这个主结论。",
            f"需要注意：{repair_path}",
        )
        )


def _question_understanding_sentence(state: WorkflowState) -> str:
    intent = state["intent"]
    metric = _business_metric_label(state)
    scope = _scope_label(intent.get("scope"))
    time_window = intent.get("time_window", "")
    question = str(state.get("request", {}).get("question") or "").strip()
    target_label = str(intent.get("target", {}).get("label") or "")
    baseline_label = str(intent.get("baseline", {}).get("label") or "")
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
    target_label = str(intent.get("target", {}).get("label") or "")
    baseline_label = str(intent.get("baseline", {}).get("label") or "")
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
        "segment_bridge": "分群结构检查",
        "segment_contribution": "渠道或分群贡献",
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
    if "established" in evidence:
        return bool(evidence.get("established"))
    return evidence.get("strength") in {"high", "medium"} and evidence.get(
        "wording_limit"
    ) in {"supported", "quantified", "contextual", "candidate"}


def _sanitize_terminal_explanation(
    explanation: dict[str, Any],
    state: WorkflowState,
    status: str,
) -> dict[str, Any]:
    value = dict(explanation or {})
    value["status"] = status
    visible_text = " ".join(
        str(value.get(key, "")) for key in ("explanation", "owner", "repair_path")
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
    owner = str(value.get("owner") or "")
    if (
        not owner
        or _has_internal_visible_token(owner)
        or _owner_drifts_to_data_quality(owner, state, status)
    ):
        value["owner"] = _terminal_owner(state, status)
    repair_path = str(value.get("repair_path") or "")
    if not repair_path or _has_internal_visible_token(repair_path):
        value["repair_path"] = _terminal_repair_path(state, status)
    if _repair_path_invents_fixed_future_window(repair_path):
        value["repair_path"] = _terminal_repair_path(state, status)
    return value


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


def _owner_drifts_to_data_quality(owner: str, state: WorkflowState, status: str) -> bool:
    return (
        any(token in owner for token in ("数据质量", "数据工程", "数据治理", "数据运营"))
        and _terminal_owner(state, status) == "业务分析负责人"
    )


def _terminal_explanation_fallback(state: WorkflowState, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "explanation": _terminal_explanation_text(state, status),
        "owner": _terminal_owner(state, status),
        "repair_path": _terminal_repair_path(state, status),
    }


def _terminal_explanation_text(state: WorkflowState, status: str) -> str:
    if state.get("clarification_outcome", {}).get("boundary_status") == "needs_question":
        return "当前不能发布业务结论。主要原因：需要先确认会影响答案的业务口径。"
    if status == "blocked":
        reasons = _business_validator_reasons(state.get("validator_results", ()))
        if not reasons:
            reasons = ("当前存在权限、合同、数据覆盖或问题边界硬限制",)
        return "当前不能发布业务结论。主要原因：" + "；".join(reasons) + "。"
    limitations = tuple(state.get("evidence_brief", {}).get("limitations", ()))
    reasons = _business_limitation_reasons(limitations)
    if not reasons:
        reasons = ("当前证据强度不足，不能支撑主业务结论",)
    return "当前降级处理，不能发布主业务结论。主要原因：" + "；".join(reasons) + "。"


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


def _terminal_owner(state: WorkflowState, status: str) -> str:
    if state.get("clarification_outcome", {}).get("boundary_status") == "needs_question":
        return "业务使用者"
    if status == "blocked":
        return "数据工程负责人"
    limitations = tuple(state.get("evidence_brief", {}).get("limitations", ()))
    if any(item in limitations for item in ("no_rows", "insufficient_values")):
        return "数据工程负责人"
    return "业务分析负责人"


def _terminal_repair_path(state: WorkflowState, status: str) -> str:
    if state.get("clarification_outcome", {}).get("boundary_status") == "needs_question":
        return "确认澄清选项，或接受推荐业务假设后继续。"
    if status == "blocked":
        return "先修复权限、合同、数据覆盖或问题边界后重跑。"
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
    if not pattern:
        return _default_claim_from_primary_evidence(state)
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


def _default_claim_from_primary_evidence(state: WorkflowState) -> dict[str, Any]:
    evidence = _primary_business_evidence(state)
    payload = evidence.get("typed_payload", {})
    capability = evidence.get("capability_id") or evidence.get("capability")
    if capability == "driver_decomposition":
        decomp = (payload.get("decompositions") or [{}])[0]
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
        findings.append(
            {
                "capability": capability,
                "business_readout": payload.get("business_readout"),
                "claim_boundary": payload.get("claim_boundary"),
                "evidence_refs": result_refs or ([evidence_ref] if evidence_ref else []),
            }
        )
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
    "decide_next_action": "判断下一步分析动作",
    "promotion_direction": "提出组合归因方向",
    "promotion_policy_gate": "组合归因门禁",
    "execute_joint_attribution": "执行组合归因",
    "interpret_evidence": "解释证据和业务含义",
    "audit_causal_implications": "审计因果和业务含义",
    "synthesize_answer": "生成业务答案草稿",
    "semantic_audit": "语义审计答案",
    "sanitize_answer": "收敛为有边界答案",
    "hard_verify_answer": "答案硬验收",
    "repair_answer": "按校验反馈修答案",
    "final_business_summary": "整理最终业务总结",
    "generate_degraded_explanation": "生成降级说明",
    "generate_blocked_explanation": "生成阻断说明",
    "persist_artifact": "保存审计结果并返回draft",
}
