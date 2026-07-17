import json
from copy import deepcopy
from dataclasses import asdict
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bi_agent.runtime import llm_client as llm_client_module
from bi_agent.runtime import langgraph_workflow as workflow_module
from bi_agent.runtime.compiler import compile_graph
from bi_agent.runtime.exploration_budget import default_budget
from bi_agent.runtime.langgraph_workflow import (
    _answer_synthesis_context,
    _analysis_runtime_request,
    _answer_quality_gate,
    _accept_analysis_route,
    _build_answer_package_from_state,
    _compiler_bound_context,
    build_available_evidence_brief,
    _apply_reused_dimension_scan_input,
    _apply_query_gap_action_to_route,
    _capability_path_labels,
    _capability_result_refs_for,
    _capability_rows_for,
    _clarification_policy_gate,
    _normalize_authority_claim_candidates,
    _default_claim_from_evidence,
    _decide_question_boundary,
    _design_analysis_route,
    _delivery_reverify_with_answer_repair,
    _execute_capabilities,
    _evidence_established,
    _execute_joint_attribution,
    _fetch_runtime_rows,
    _final_business_summary,
    _final_summary_needs_display_repair,
    _merge_confirmed_material_requirements,
    _local_coverage_answerable_reason,
    _infer_question_families_from_requested_nodes,
    _business_query_gap_projection,
    _business_query_repair_gap,
    _reconcile_route_metric_capabilities,
    _normalize_evidence_interpretation_output,
    _question_family_values,
    normalize_final_answer_audit,
    _generate_query_gap_clarification,
    _generate_degraded_explanation,
    _group_query_gap_actions,
    _preserved_authority_claims,
    _persist_clarification,
    _persist_query_gap_clarification,
    _retrying_node,
    _align_route_output_to_requested,
    _normalize_route_requested_nodes,
    _repair_path_invents_fixed_future_window,
    _repair_analysis_contract,
    _render_query_gap_actions,
    _reduce_evidence,
    evaluate_answer_quality,
    _route_after_next_action,
    _route_after_query_gap_clarification,
    _route_after_query_repair,
    _route_after_clarification,
    _route_after_accept_analysis,
    _route_after_semantic_audit,
    _sanitize_terminal_explanation,
    _segment_contribution_params,
    _understand_business_intent,
    _typed_clarification_compiled_graph,
    _validate_runtime_binding,
    WorkflowFailure,
    run_pattern_workflow as _run_pattern_workflow,
)

from bi_agent.runtime.analysis_runtime import (
    AnalysisRuntime,
    AnalysisRuntimeRequest,
    AnalysisRuntimeResult,
    AnswerPackageBuildContext,
    analysis_outcome_has_executable_ready_capability,
    analysis_outcome_requires_route_clarification,
    analysis_outcome_requires_preexecution_clarification,
)
from bi_agent.runtime.analysis_contracts import (
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    ContractGap,
)
from bi_agent.runtime.llm_client import (
    LLMConfigurationError,
    LLMOutputError,
    LLMTimeoutError,
    OpenAICompatibleLLMClient,
    _localize_narrative_fields,
)
from bi_agent.runtime.llm_prompts import build_prompt, validate_prompt_specs
from bi_agent.runtime.data_contract_diagnostics import diagnose_contract_gaps
from tests.support.scripted_llm import ScriptedLLMClient
from tests.support.scripted_llm import ScriptedLLMResult
from tests.phase7.test_analysis_goal_registry import EMPTY_FOCUS, EXPLAIN_CHANGE


class _SequencedJSONCompletions:
    def __init__(self, outputs):
        self.outputs = [dict(output) for output in outputs]
        self.attempt_count = 0

    def create(self, **kwargs):
        index = min(self.attempt_count, len(self.outputs) - 1)
        output = self.outputs[index]
        self.attempt_count += 1
        message = type(
            "SequencedMessage",
            (),
            {"content": json.dumps(output, ensure_ascii=False)},
        )()
        choice = type("SequencedChoice", (), {"message": message})()
        return type(
            "SequencedResponse",
            (),
            {
                "id": f"sequenced-response-{self.attempt_count}",
                "choices": [choice],
                "usage": None,
            },
        )()


def _provider_client_with_outputs(outputs):
    completions = _SequencedJSONCompletions(outputs)
    client = OpenAICompatibleLLMClient(
        provider="openai_compatible",
        model="pattern-contract-model",
        api_key="test-key",
    )
    client._client = type(
        "SequencedClient",
        (),
        {
            "chat": type(
                "SequencedChat",
                (),
                {"completions": completions},
            )()
        },
    )()
    return client, completions


def _claim_fixture(
    *,
    text,
    evidence_refs,
    numbers,
    scope="full_sample",
    time_window="2024-01..2026-05",
    claim_type="recurring_pattern_existence",
    claim_strength="observed",
):
    return {
        "text": text,
        "evidence_refs": list(evidence_refs),
        "numbers": dict(numbers),
        "scope": scope,
        "time_window": time_window,
        "claim_type": claim_type,
        "claim_strength": claim_strength,
    }


def _provider_business_intent_output(**overrides):
    output = {
        "question_family": "revenue_health_review",
        "target_metric": "paid_amount",
        "pattern_family": "rolling",
        "pattern_params": {},
        "scope": "full_sample",
        "time_window": "2026-06-02",
        "target_claim": "检查当前付费金额经营表现",
        "baseline_candidates": [],
        "analysis_requirements": {
            "goal_bindings": deepcopy(EXPLAIN_CHANGE),
            "explicit_focus": deepcopy(EMPTY_FOCUS),
        },
        "status_message": "已完成业务意图识别。",
        "display_summary": "已绑定业务问题与分析窗口。",
    }
    output.update(overrides)
    return output


def _provider_analysis_route_output(**overrides):
    output = {
        "requested_nodes": ["rolling_window_compare"],
        "route_summary": (
            "先复核目标窗口，再用滚动窗口对比核对变化是否持续。"
        ),
        "expected_evidence": {
            "rolling_window_compare": "目标窗口与滚动基线之间的可比变化证据。",
        },
        "analysis_requirements": {
            "target_metrics": ["paid_amount"],
            "baselines": ["rolling_7_day_baseline"],
            "context_sources": [],
            "dataset_requirements": [],
            "diagnostic_tags": [],
            "scope": {"type": "full_sample"},
        },
        "decision_summary": "保留可执行的滚动窗口验证路径。",
        "display_summary": "已形成滚动窗口核对路线。",
    }
    output.update(overrides)
    requested_nodes = list(output.get("requested_nodes") or [])
    output.setdefault(
        "capability_sections",
        {
            capability: {
                "route_step": "核对该业务能力对应的分析路径。",
                "expected_evidence": str(
                    (output.get("expected_evidence") or {}).get(capability)
                    or "核对该能力对应的业务证据与限制。"
                ),
            }
            for capability in requested_nodes
        },
    )
    output["expected_evidence"] = {
        capability: str(section["expected_evidence"])
        for capability, section in output["capability_sections"].items()
        if capability in requested_nodes
    }
    output.setdefault(
        "narrative_capability_refs",
        {
            "route_summary_capability_ids": requested_nodes,
            "decision_summary_capability_ids": requested_nodes,
            "display_summary_capability_ids": requested_nodes,
            "expected_evidence_capability_ids": {
                capability: [capability]
                for capability in requested_nodes
            },
        },
    )
    return output


def _provider_capability_sections(
    requested_nodes,
    *,
    route_step="核对该能力对应的业务路径。",
    expected_evidence="核对该能力对应的业务证据与限制。",
):
    return {
        capability: {
            "route_step": route_step,
            "expected_evidence": expected_evidence,
        }
        for capability in requested_nodes
    }


def _provider_final_route_narrative_output(
    requested_nodes,
    *,
    route_summary="先核对真实变化方向，再沿已确定路线检查相关因素。",
    decision_summary="路线保留查询前尚未验证的方向和证据边界。",
    display_summary="分析路线已经确定，下一步核验数据。",
    sections=None,
):
    if sections is None:
        sections = [
            {
                "step_ref": f"step_{index}",
                "route_step": "核对该步骤对应的业务问题。",
                "expected_evidence": "获得该步骤对应的业务证据与限制说明。",
            }
            for index, _ in enumerate(requested_nodes, start=1)
        ]
    return {
        "route_summary": route_summary,
        "sections": sections,
        "decision_summary": decision_summary,
        "display_summary": display_summary,
    }


def _provider_closed_analysis_route_output(**overrides):
    requested_nodes = (
        "rolling_window_compare",
        "metric_timeseries",
        "data_quality_profile",
        "evidence_reduce",
        "answer_verify",
        "compare_periods",
        "compare_period_phases",
        "weekday_calendar_compare",
    )
    output = _provider_analysis_route_output(
        requested_nodes=list(requested_nodes),
        expected_evidence={
            capability: "该业务能力对应的可验证证据与限制说明。"
            for capability in requested_nodes
        },
        analysis_requirements={
            "target_metrics": ["paid_amount"],
            "baselines": ["rolling_7_day_baseline"],
            "context_sources": [],
            "dataset_requirements": ["paid_order_success"],
            "diagnostic_tags": [],
            "scope": "full_sample",
        },
    )
    output.update(overrides)
    return output


def _provider_query_gap_clarification_output(
    options,
    *,
    recommended=None,
):
    business_options = [str(option) for option in options]
    recommended_option = str(recommended or business_options[0])
    return {
        "questions": [
            {
                "question": "需要确认按哪个业务口径继续？",
                "options": [
                    *business_options,
                    "tell the agent to do differently",
                ],
            }
        ],
        "recommended_assumption": {"option": recommended_option},
        "recommendation_reason": "该选择保留当前可验证结论并明确数据边界。",
        "decision_summary": "该选择会影响后续可发布的业务结论。",
        "display_summary": "需要确认数据缺口的处理方式。",
    }


def _provider_analysis_route_state(client):
    registry = workflow_module.RuntimeContractRegistry.from_path(
        workflow_module.CANONICAL_RUNTIME_BINDINGS_PATH
    )
    goal_material = workflow_module._bind_compiled_analysis_plan(
        {
            "target_metric": "paid_amount",
            "goal_bindings": deepcopy(EXPLAIN_CHANGE),
            "explicit_focus": deepcopy(EMPTY_FOCUS),
        },
        registry,
    )
    return {
        "run_id": "provider-route-contract",
        "request": {
            "run_id": "provider-route-contract",
            "question": "近七日付费金额变化是否持续？",
            "run_mode": "production",
        },
        "intent": {
            **goal_material,
            "question_family": "pattern_explanation",
            "question_families": ["pattern_explanation"],
            "primary_question_family": "pattern_explanation",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
            "target_metrics": ["paid_amount"],
            "pattern_family": "rolling",
            "pattern_params": {},
            "scope": "full_sample",
            "time_window": "2026-05-26..2026-06-02",
            "target_claim": "baseline_stability",
            "baseline_candidates": ["rolling_7_day_baseline"],
        },
        "confirmed_understanding": {},
        "llm_client": client,
        "llm_calls": [],
        "checkpoint_events": [],
    }


def _test_terminal_execution_material():
    from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    return {
        "schema_version": "3",
        "target_semantic": "2026-06-02",
        "as_of": "2026-06-03T12:00:00+01:00",
        "business_timezone": "Africa/Lagos",
        "context_window_specs": [],
        "fixed_window_bounds": {
            "target_day": ["2026-06-02", "2026-06-02"],
        },
        "filters": [],
        "grain": "window_id",
        "dataset_requirements": [],
        "metric_dataset_overrides": {},
        "dimension_dataset_overrides": {},
        "requested_context_sources": [],
        "accepted_graph": ["compare_periods"],
        "runtime_contract_version": registry.contract_version,
        "runtime_registry_digest": registry.source_payload_digest,
        "run_mode_class": "authoritative",
        "source_query_contracts": [],
    }


def _claim_ready_bound(binding_ref):
    query_ref = f"query:{binding_ref}"
    return type(
        "ClaimReadyBound",
        (),
        {
            "status": "ready",
            "binding_manifest_ref": binding_ref,
            "input_completeness_statuses": ("complete",),
            "query_contract_refs": (query_ref,),
            "validation_query_contract_refs": (),
            "plan_payload": {
                "required_input_slots": (
                    {
                        "slot_id": "primary",
                        "query_contract_refs": (query_ref,),
                    },
                ),
                "optional_input_slots": (),
                "minimum_readiness": {"required_slots": "all"},
            },
            "binding_payload": {},
        },
    )()


def _capability_plan_fixture(
    capability_id,
    *,
    required_query_refs,
    optional_query_refs=(),
    required_mode="all",
):
    def slot(slot_id, query_refs, required):
        return CapabilityInputSlot(
            slot_id=slot_id,
            query_contract_refs=tuple(query_refs),
            required=required,
            accepted_completeness=("complete",),
            required_fields=(),
            required_window_ids=(),
            validation_query_contract_refs=(),
        )

    return CapabilityExecutionPlan(
        capability_id=capability_id,
        capability_contract_ref=f"capability:{capability_id}",
        required_input_slots=tuple(
            slot(f"required-{index}", refs, True)
            for index, refs in enumerate(required_query_refs, start=1)
        ),
        optional_input_slots=tuple(
            slot(f"optional-{index}", refs, False)
            for index, refs in enumerate(optional_query_refs, start=1)
        ),
        merge_strategy="independent",
        minimum_readiness={
            "required_slots": required_mode,
            "accepted_completeness": ("complete",),
        },
        degradation_policy={},
        supported_evidence_types=("observed",),
        maximum_claim_strength="descriptive",
        supported_claim_types=("comparative_change",),
    )


def scripted_provider_request(config, messages):
    return {
        "response_id": "subprocess-response",
        "content": '{"ok": true}',
        "usage": {},
    }


def spawn_safe_stuck_llm_request(config, messages):
    time.sleep(2)
    return {
        "response_id": "too-late",
        "content": '{"ok": true}',
        "usage": {},
    }


def large_scripted_provider_request(config, messages):
    size = int(config.get("payload_size", 20_000))
    return {f"key-{index:06d}": "value" for index in range(size)}


def spawn_failing_llm_request(config, messages):
    raise RuntimeError("provider-worker-failed")


def spawn_exit_without_llm_result(config, messages):
    os._exit(7)


class SpawnTimeoutThenSuccessWorker:
    def __call__(self, config, messages):
        attempt = int(config.get("attempt", 1))
        if attempt == 1:
            time.sleep(2)
        return {
            "response_id": "subprocess-retry-success",
            "content": '{"ok": true}',
            "usage": {},
        }


def _llm_input_payload(answer_package, task):
    call = next(
        item for item in answer_package["admin_audit"]["llm_calls"] if item["task"] == task
    )
    user_message = next(item for item in call["messages"] if item["role"] == "user")
    content = user_message["content"]
    start = content.index("<input_json>") + len("<input_json>")
    end = content.index("</input_json>")
    return json.loads(content[start:end].strip())


def _required_claim_resolution_state():
    return {
        "request": {"run_mode": "production"},
        "checkpoint_events": [{"node": "decide_next_action"}],
        "intent": {
            "question_family": "paid_amount_change_explanation",
            "target_metric": "paid_amount",
            "pattern_family": "custom_baseline",
            "scope": "full_sample",
            "time_window": "2026-06-01",
            "required_claim_intents": [
                "comparative_change",
                "formula_component_contribution",
            ],
            "candidate_claim_intents": ["baseline_stability"],
            "claim_intents": [
                "comparative_change",
                "formula_component_contribution",
                "baseline_stability",
            ],
            "baseline": {"label": "2026-05-31"},
            "target": {"label": "2026-06-01"},
        },
        "evidence": [
            {
                "evidence_ref": "compare_periods:ready",
                "capability_id": "compare_periods",
                "claim_type": "comparative_change",
                "claim_input_ready": True,
                "binding_manifest_ref": "binding:compare",
                "evidence_type": "statistical_association",
                "supported_evidence_types": ["statistical_association"],
                "supported_claim_types": ["comparative_change"],
                "maximum_claim_strength": "directional",
                "maximum_claim_strength_rank": 1,
                "strength": "directional",
                "wording_limit": "quantified",
                "limitations": [],
                "typed_payload": {
                    "target_value": 308_240_309,
                    "baseline_value": 304_142_630,
                    "absolute_change": 4_097_679,
                    "relative_change": 0.013472886060069909,
                },
            },
            {
                "evidence_ref": "rolling_window_compare:weak",
                "capability_id": "rolling_window_compare",
                "claim_type": "baseline_stability",
                "claim_input_ready": True,
                "binding_manifest_ref": "binding:rolling",
                "evidence_type": "insufficient",
                "supported_evidence_types": ["statistical_association"],
                "supported_claim_types": ["baseline_stability"],
                "maximum_claim_strength": "directional",
                "maximum_claim_strength_rank": 1,
                "strength": "low",
                "wording_limit": "blocked",
                "limitations": ["weak_direction", "below_materiality_floor"],
                "typed_payload": {
                    "pattern_family": "custom_baseline",
                    "comparable_periods": 1,
                    "median_uplift": 0.01,
                },
            },
            {
                "evidence_ref": "formula_decompose:auxiliary-gaps",
                "capability_id": "formula_decompose",
                "claim_type": "formula_component_contribution",
                "claim_input_ready": True,
                "binding_manifest_ref": "binding:formula",
                "evidence_type": "accounting_contribution",
                "supported_evidence_types": ["accounting_contribution"],
                "supported_claim_types": ["formula_component_contribution"],
                "maximum_claim_strength": "quantified_contribution",
                "maximum_claim_strength_rank": 3,
                "strength": "low",
                "wording_limit": "degraded",
                "limitations": [
                    "missing_formula_component:payment_success_chain",
                    "missing_formula_dimension:region_sum",
                ],
                "typed_payload": {
                    "gaps": [
                        {
                            "formula_id": "payment_success_chain",
                            "candidate_role": "auxiliary_candidate",
                            "candidate_status": "blocked",
                        }
                    ]
                },
            },
            {
                "evidence_ref": "driver_decomposition:ready",
                "capability_id": "driver_decomposition",
                "claim_type": "formula_component_contribution",
                "claim_input_ready": True,
                "binding_manifest_ref": "binding:driver",
                "evidence_type": "accounting_contribution",
                "supported_evidence_types": ["accounting_contribution"],
                "supported_claim_types": ["formula_component_contribution"],
                "maximum_claim_strength": "quantified_contribution",
                "maximum_claim_strength_rank": 3,
                "strength": "high",
                "wording_limit": "quantified",
                "limitations": [],
                "typed_payload": {
                    "core_reconciliation_status": "reconciled",
                    "primary_core_driver": "avg_order_amount",
                    "amount_delta_ratio": 0.013472886060069909,
                    "avg_order_amount_contribution_share": 1.2622775809926496,
                    "paid_frequency_contribution_share": -0.28206726717258257,
                    "paid_users_contribution_share": 0.01978968617993287,
                    "decompositions": [
                        {
                            "primary_core_driver": "avg_order_amount",
                            "core_reconciliation_status": "reconciled",
                            "core_factor_contributions": [
                                {
                                    "component_id": "avg_order_amount",
                                    "contribution_share": 1.2622775809926496,
                                },
                                {
                                    "component_id": "paid_frequency",
                                    "contribution_share": -0.28206726717258257,
                                },
                                {
                                    "component_id": "paid_users",
                                    "contribution_share": 0.01978968617993287,
                                },
                            ],
                            "payment_success_assumption": {
                                "observed": False,
                                "status": "assumed_neutral",
                            },
                        }
                    ],
                },
            },
        ],
    }


def _current_required_claim_resolution_state():
    state = _required_claim_resolution_state()
    intent = state["intent"]
    for legacy_field in (
        "required_claim_intents",
        "candidate_claim_intents",
        "claim_intents",
    ):
        intent.pop(legacy_field, None)
    intent.update(
        {
            "required_claim_types": [
                "comparative_change",
                "formula_component_contribution",
            ],
            "auxiliary_claim_types": ["baseline_stability"],
            "publishable_claim_types": [
                "comparative_change",
                "formula_component_contribution",
                "baseline_stability",
            ],
        }
    )
    return state


class LLMWorkflowTest(unittest.TestCase):
    def test_evidence_interpretation_receives_business_projection_only(self):
        state = _required_claim_resolution_state()
        state.update(
            {
                "run_id": "business-evidence-projection",
                "llm_client": ScriptedLLMClient(
                    {
                        "evidence_interpretation": {
                            "interpretation": "业务证据支持当前对比结论。",
                            "decision_summary": "保留已验证结论和局部边界。",
                            "evidence_boundary": "支付成功率缺少独立观测。",
                        }
                    }
                ),
                "llm_calls": [],
            }
        )
        _reduce_evidence(state)

        workflow_module._interpret_evidence(state)

        call = next(
            item
            for item in state["llm_calls"]
            if item["task"] == "evidence_interpretation"
        )
        user_message = next(
            item for item in call["messages"] if item["role"] == "user"
        )
        payload = json.loads(
            user_message["content"]
            .split("<input_json>", 1)[1]
            .split("</input_json>", 1)[0]
            .strip()
        )
        self.assertEqual(set(payload), {"businessContext"})
        visible = json.dumps(payload, ensure_ascii=False)
        self.assertIn("付费金额", visible)
        for internal in (
            "evidence_ref",
            "capability_id",
            "driver_decomposition",
            "formula_component_contribution",
            "avg_order_amount",
        ):
            self.assertNotIn(internal, visible)

    def test_answer_repair_rewrites_prose_without_mutating_authority_claims(self):
        state = _required_claim_resolution_state()
        for evidence in state["evidence"]:
            if evidence["capability_id"] == "compare_periods":
                evidence["numeric_facts"] = {
                    "target_value": 120.0,
                    "baseline_value": 100.0,
                    "absolute_change": 20.0,
                    "relative_change": 0.2,
                }
            elif evidence["capability_id"] == "driver_decomposition":
                evidence["numeric_facts"] = {
                    "paid_users_contribution": 2.0,
                    "paid_frequency_contribution": -4.0,
                    "avg_order_amount_contribution": 22.0,
                    "formula_contribution_total": 20.0,
                }
        fake = ScriptedLLMClient(
            {
                "answer_repair": {
                    "answer_text": "修复后的业务文案继续保留权威事实。",
                }
            }
        )
        state.update(
            {
                "run_id": "authority-claim-repair",
                "llm_client": fake,
                "llm_calls": [],
                "evidence_interpretation": {},
                "causal_audit": {},
                "analysis_route": {},
                "semantic_audit": {
                    "audit_status": "needs_revision",
                    "issues": [{"severity": "error", "description": "措辞过强"}],
                },
                "verifier": {"errors": []},
                "retry_context": {
                    "failed_node": "semantic_audit",
                    "failure_type": "semantic_audit",
                    "failure_reason": "措辞过强",
                },
            }
        )
        _reduce_evidence(state)
        state["draft_claims"] = workflow_module._authority_claims_from_evidence(state)
        original_claims = deepcopy(state["draft_claims"])

        workflow_module._repair_answer(state)

        self.assertEqual(state["draft_claims"], original_claims)
        repair_call = next(
            call for call in state["llm_calls"] if call["task"] == "answer_repair"
        )
        self.assertNotIn("claims", repair_call["required_keys"])
        user_message = next(
            item for item in repair_call["messages"] if item["role"] == "user"
        )
        payload = json.loads(
            user_message["content"]
            .split("<input_json>", 1)[1]
            .split("</input_json>", 1)[0]
            .strip()
        )
        self.assertEqual(
            set(payload),
            {"answerText", "businessContext", "displayReview"},
        )
        self.assertTrue(payload["displayReview"]["findings"])
        self.assertIn(
            "需要修正",
            "".join(payload["displayReview"]["findings"]),
        )
        visible = json.dumps(payload, ensure_ascii=False)
        for internal in (
            "draft_claims",
            "evidence_ref",
            "driver_decomposition",
            "formula_component_contribution",
            "retry_context",
        ):
            self.assertNotIn(internal, visible)

    def test_query_gap_execution_material_survives_langgraph_node_boundary(self):
        from langgraph.graph import END, StateGraph

        material = _test_terminal_execution_material()
        graph = StateGraph(workflow_module.WorkflowState)

        def compile_runtime(_state):
            return {"execution_material": material}

        def persist_query_gap(state):
            return {
                "answer_package": {
                    "execution_material": state.get("execution_material")
                }
            }

        graph.add_node("compile_runtime", compile_runtime)
        graph.add_node("persist_query_gap", persist_query_gap)
        graph.set_entry_point("compile_runtime")
        graph.add_edge("compile_runtime", "persist_query_gap")
        graph.add_edge("persist_query_gap", END)

        result = graph.compile().invoke({})

        self.assertEqual(
            result["answer_package"]["execution_material"],
            material,
        )

    def test_question_family_normalization_uses_bound_primary_for_supported_diagnostic(self):
        normalized = workflow_module._normalize_question_families(
            {
                "question_family": "revenue_health",
                "primary_question_family": "paid_amount_change_explanation",
                "question_families": ["revenue_health"],
            }
        )

        self.assertEqual(
            normalized["question_family"],
            "paid_amount_change_explanation",
        )
        self.assertEqual(
            normalized["question_families"],
            ["paid_amount_change_explanation"],
        )
        self.assertEqual(normalized["secondary_question_families"], [])

    def test_question_family_normalization_rejects_ambiguous_diagnostic_without_binding(self):
        with self.assertRaisesRegex(
            WorkflowFailure,
            "diagnostic_question_family_ambiguous:multi_baseline",
        ):
            workflow_module._normalize_question_families(
                {"question_family": "multi_baseline"}
            )

    def test_question_family_normalization_rejects_diagnostic_mismatched_to_bound_primary(self):
        with self.assertRaisesRegex(
            WorkflowFailure,
            "diagnostic_question_family_incompatible:multi_baseline:revenue_health_review",
        ):
            workflow_module._normalize_question_families(
                {
                    "question_family": "multi_baseline",
                    "primary_question_family": "revenue_health_review",
                }
            )

    def test_question_family_normalization_rejects_unknown_provider_token(self):
        with self.assertRaisesRegex(
            WorkflowFailure,
            "unknown_question_family_or_diagnostic:model_invented_family",
        ):
            workflow_module._normalize_question_families(
                {"question_family": "model_invented_family"}
            )

    def test_question_family_normalization_rejects_conflicting_canonical_primary_candidates(self):
        with self.assertRaisesRegex(
            WorkflowFailure,
            "question_family_primary_conflict:pattern_explanation,revenue_health_review",
        ):
            workflow_module._normalize_question_families(
                {
                    "primary_question_family": "revenue_health_review",
                    "question_family": "pattern_explanation",
                }
            )

    def test_question_family_normalization_rejects_conflicting_unique_diagnostics(self):
        with self.assertRaisesRegex(
            WorkflowFailure,
            "question_family_primary_conflict:business_object_impact_review,paid_amount_change_explanation",
        ):
            workflow_module._normalize_question_families(
                {
                    "primary_question_family": "driver_focus",
                    "question_family": "event_impact",
                }
            )

    def test_question_family_normalization_deduplicates_same_result_candidates(self):
        normalized = workflow_module._normalize_question_families(
            {
                "primary_question_family": "driver_focus",
                "question_family": "driver_focus",
                "question_families": ["driver_focus"],
            }
        )

        self.assertEqual(
            normalized["primary_question_family"],
            "paid_amount_change_explanation",
        )
        self.assertEqual(
            normalized["question_families"],
            ["paid_amount_change_explanation"],
        )

    def test_question_family_normalization_is_idempotent_for_canonical_families(self):
        intent = {
            "question_family": "custom_baseline_comparison",
            "primary_question_family": "custom_baseline_comparison",
            "question_families": [
                "custom_baseline_comparison",
                "data_quality_or_evidence_review",
            ],
            "secondary_question_families": ["data_quality_or_evidence_review"],
        }

        normalized = workflow_module._normalize_question_families(intent)

        self.assertEqual(
            workflow_module._normalize_question_families(normalized),
            normalized,
        )

    def test_business_intent_rejects_non_mapping_optional_contract_as_typed_llm_failure(self):
        state = {
            "request": {"question": "检查业务规律"},
            "llm_client": ScriptedLLMClient({
                "business_intent": {
                    "question_family": "pattern_explanation",
                    "target_metric": "paid_amount",
                    "pattern_family": "weekly",
                    "scope": "full_sample",
                    "time_window": "recent_period",
                    "target_claim": "是否存在规律",
                    "baseline_candidates": [],
                    "status_message": "已识别",
                    "display_summary": "已识别",
                    "answer_contract": ["malformed"],
                }
            }),
            "llm_calls": [],
            "checkpoint_events": [],
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            "business_intent_contract_invalid:answer_contract",
        ):
            _understand_business_intent(state)

    def test_business_intent_rejects_falsey_non_mapping_optional_contract(self):
        for answer_contract in (None, "", [], 0, False):
            with self.subTest(answer_contract=answer_contract):
                state = {
                    "request": {"question": "检查一类业务规律"},
                    "llm_client": ScriptedLLMClient({
                        "business_intent": {
                            "answer_contract": answer_contract,
                        }
                    }),
                    "llm_calls": [],
                    "checkpoint_events": [],
                }

                with self.assertRaisesRegex(
                    WorkflowFailure,
                    "business_intent_contract_invalid:answer_contract",
                ):
                    _understand_business_intent(state)

    def test_business_intent_contract_failure_is_not_retried_by_workflow_node(self):
        class StringThenEmptyContractLLM(ScriptedLLMClient):
            def __init__(self):
                super().__init__(
                    {
                        "business_intent": _provider_business_intent_output(
                            answer_contract={}
                        )
                    }
                )
                self.attempts = 0
                self.message_batches = []

            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                self.attempts += 1
                self.message_batches.append([dict(message) for message in messages])
                result = super().invoke_json(
                    task=task,
                    prompt_version=prompt_version,
                    messages=messages,
                    required_keys=required_keys,
                )
                result.output["answer_contract"] = (
                    "direct answer" if self.attempts == 1 else {}
                )
                return result

        fake = StringThenEmptyContractLLM()
        state = {
            "request": {"question": "检查一类业务规律"},
            "llm_client": fake,
            "llm_calls": [],
            "checkpoint_events": [],
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            "business_intent_contract_invalid:answer_contract",
        ):
            _retrying_node(
                "understand_business_intent", _understand_business_intent
            )(state)

        self.assertEqual(fake.attempts, 1)
        self.assertEqual(
            [event["status"] for event in state["checkpoint_events"]],
            ["failed"],
        )

    def test_business_intent_answer_contract_stays_fail_closed_after_one_node_call(
        self,
    ):
        class AlwaysStringContractLLM(ScriptedLLMClient):
            def __init__(self):
                super().__init__(
                    {
                        "business_intent": _provider_business_intent_output(
                            answer_contract={}
                        )
                    }
                )
                self.attempts = 0

            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                self.attempts += 1
                result = super().invoke_json(
                    task=task,
                    prompt_version=prompt_version,
                    messages=messages,
                    required_keys=required_keys,
                )
                result.output["answer_contract"] = "direct answer"
                return result

        fake = AlwaysStringContractLLM()
        state = {
            "request": {"question": "检查一类业务规律"},
            "llm_client": fake,
            "llm_calls": [],
            "checkpoint_events": [],
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            "business_intent_contract_invalid:answer_contract",
        ):
            _retrying_node(
                "understand_business_intent", _understand_business_intent
            )(state)

        self.assertEqual(fake.attempts, 1)

    def test_production_business_intent_rejects_missing_material_without_local_defaults(self):
        base = {
            "question_family": "pattern_explanation",
            "target_metric": "paid_amount",
            "pattern_family": "intra_period",
            "scope": "full_sample",
            "time_window": "2026-06-02",
            "target_claim": "recurring_pattern_existence",
            "baseline_candidates": [],
            "analysis_requirements": {
                "context_sources": [],
                "claim_intents": [],
                "requested_dimensions": [],
                "requested_components": [],
            },
            "answer_contract": {},
        }
        for axis in (
            "question_family",
            "target_metric",
            "pattern_family",
            "scope",
            "time_window",
            "target_claim",
        ):
            with self.subTest(axis=axis):
                output = {**base, axis: ""}
                state = {
                    "request": {
                        "question": "检查昨天的业务变化",
                        "run_mode": "production",
                    },
                    "llm_client": ScriptedLLMClient({"business_intent": output}),
                    "llm_calls": [],
                    "checkpoint_events": [],
                }

                with self.assertRaisesRegex(
                    WorkflowFailure,
                    f"business_intent_contract_invalid:{axis}",
                ):
                    _understand_business_intent(state)

    def test_production_business_intent_does_not_fill_ignored_time_recommendation(self):
        output = {
            "question_family": "data_quality_or_evidence_review",
            "target_metric": "paid_amount",
            "pattern_family": "none",
            "scope": "full_sample",
            "time_window": None,
            "target_claim": "确认现有证据边界",
            "baseline_candidates": [],
            "analysis_requirements": {
                "context_sources": [],
                "claim_intents": ["contract_coverage_and_trust_boundary"],
                "requested_dimensions": [],
                "requested_components": [],
            },
            "answer_contract": {},
        }
        state = {
            "request": {
                "question": "请按系统建议的时间口径检查证据。",
                "run_mode": "production",
                "analysis_context": {
                    "as_of": "2026-06-03T12:00:00+01:00",
                    "target_date": "2026-06-02",
                },
            },
            "llm_client": ScriptedLLMClient({"business_intent": output}),
            "llm_calls": [],
            "checkpoint_events": [],
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            "^business_intent_contract_invalid:time_window$",
        ):
            _understand_business_intent(state)

    def test_production_business_intent_does_not_infer_weekly_params_from_question_text(self):
        state = {
            "request": {
                "question": "周末表现是否不同？",
                "run_mode": "live",
            },
            "llm_client": ScriptedLLMClient({
                "business_intent": {
                    "question_family": "pattern_explanation",
                    "target_metric": "paid_amount",
                    "pattern_family": "weekly",
                    "pattern_params": {},
                    "scope": "full_sample",
                    "time_window": "2026-05-26..2026-06-02",
                    "target_claim": "recurring_pattern_existence",
                    "baseline_candidates": [],
                    "analysis_requirements": {
                        "context_sources": [],
                        "claim_intents": [],
                        "requested_dimensions": [],
                        "requested_components": [],
                    },
                    "answer_contract": {},
                }
            }),
            "llm_calls": [],
            "checkpoint_events": [],
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            "business_intent_contract_invalid:pattern_params",
        ):
            _understand_business_intent(state)

    def test_production_business_intent_rejects_uninterpretable_scope_mapping(self):
        output = {
            "question_family": "pattern_explanation",
            "target_metric": "paid_amount",
            "pattern_family": "intra_period",
            "pattern_params": {"target_phase": "month_start"},
            "scope": {"segment": "vip"},
            "time_window": "2026-01-01..2026-06-02",
            "target_claim": "recurring_pattern_existence",
            "baseline_candidates": [],
            "analysis_requirements": {
                "context_sources": [],
                "claim_intents": [],
                "requested_dimensions": [],
                "requested_components": [],
            },
            "answer_contract": {},
        }
        state = {
            "request": {"question": "检查 VIP 用户规律", "run_mode": "production"},
            "llm_client": ScriptedLLMClient({"business_intent": output}),
            "llm_calls": [],
            "checkpoint_events": [],
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            "business_intent_contract_invalid:scope",
        ):
            _understand_business_intent(state)

    def test_production_business_intent_rejects_non_json_time_window_shape(self):
        output = {
            "question_family": "pattern_explanation",
            "target_metric": "paid_amount",
            "pattern_family": "intra_period",
            "pattern_params": {"target_phase": "month_start"},
            "scope": {"type": "full_sample"},
            "time_window": {"target": object()},
            "target_claim": "recurring_pattern_existence",
            "baseline_candidates": [],
            "analysis_requirements": {
                "context_sources": [],
                "claim_intents": [],
                "requested_dimensions": [],
                "requested_components": [],
            },
            "answer_contract": {},
        }
        state = {
            "request": {"question": "检查业务规律", "run_mode": "live"},
            "llm_client": ScriptedLLMClient({"business_intent": output}),
            "llm_calls": [],
            "checkpoint_events": [],
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            "business_intent_contract_invalid:time_window",
        ):
            _understand_business_intent(state)

    def test_production_business_intent_keeps_reviewed_baseline_compatibility_shapes(self):
        self.assertEqual(
            workflow_module._validated_business_intent_baseline_candidates(
                [
                    {"type": "same_weekday", "lag_weeks": 1},
                    "前一天",
                    {"type": "rolling_average", "window": 7},
                ],
                production_like=True,
            ),
            [
                "same_weekday_last_week",
                "previous_day",
                "rolling_7_day_baseline",
            ],
        )

    def test_business_intent_payload_uses_registry_authoritative_baseline_vocabulary(self):
        payload = workflow_module._business_intent_payload(
            {
                "question": "将近七日均值和前一天作为对比基线。",
                "run_mode": "production",
                "allowed_baseline_ids": ["forged_baseline"],
                "scenario": {"baselines": ["forged_baseline"]},
            }
        )

        self.assertEqual(
            payload["allowed_baseline_ids"],
            [
                "previous_day",
                "rolling_7_day_baseline",
                "same_weekday_last_week",
            ],
        )
        vocabulary = payload["allowed_baseline_semantics"]
        self.assertEqual(
            [item["id"] for item in vocabulary],
            payload["allowed_baseline_ids"],
        )
        self.assertTrue(all(item["label"] for item in vocabulary))
        self.assertTrue(all(item["semantics"] for item in vocabulary))
        self.assertNotIn("forged_baseline", json.dumps(vocabulary, ensure_ascii=False))

    def test_business_intent_payload_uses_reviewed_public_scope_vocabulary(self):
        payload = workflow_module._business_intent_payload(
            {
                "question": "查看当前总体经营情况。",
                "run_mode": "production",
                "allowed_scope_types": ["forged_scope"],
                "scenario": {"scope": "forged_scope"},
            }
        )

        self.assertEqual(payload["allowed_scope_types"], ["full_sample"])

    def test_business_intent_payload_exposes_fixed_target_as_reviewed_time_recommendation(self):
        payload = workflow_module._business_intent_payload(
            {
                "question": "请按系统推荐的时间口径检查经营证据。",
                "run_mode": "production",
                "analysis_context": {
                    "as_of": "2026-06-03T12:00:00+01:00",
                    "target_date": "2026-06-02",
                    "previous_day": "2026-06-01",
                },
            }
        )

        self.assertEqual(
            payload["reviewed_time_window_recommendation"],
            {
                "time_window": "2026-06-02",
                "source": "analysis_context.target_date",
            },
        )

    def test_business_intent_payload_rejects_malformed_time_recommendation_source(self):
        malformed_values = (
            None,
            "2026-02-30",
            "2026-6-2",
            "2026-06-02T00:00:00+00:00",
            "20260602",
            20260602,
        )

        for target_date in malformed_values:
            with self.subTest(target_date=target_date), self.assertRaisesRegex(
                WorkflowFailure,
                "^business_intent_analysis_context_invalid:target_date$",
            ):
                workflow_module._business_intent_payload(
                    {
                        "question": "请推荐时间口径。",
                        "run_mode": "production",
                        "analysis_context": {"target_date": target_date},
                    }
                )

    def test_prior_topic_material_projects_only_verified_bound_business_context(self):
        from tests.phase7.test_conversation_runtime import _seed_runtime

        turn = _seed_runtime().handle_message(
            "thread-phase7",
            "继续看刚才的渠道贡献。",
        )
        request = turn.run_request.to_dict()
        payload = workflow_module._business_intent_payload(request)
        intent_material = request["prior_topic_material_context"][
            "material_projection"
        ]["intent_material"]

        self.assertEqual(
            payload["bound_business_context"],
            {
                "target_metric": intent_material["primary_target_metric"],
                "scope": intent_material["scope"],
                "time_window": intent_material["time_window"],
                "prior_baselines": intent_material["baselines"],
            },
        )
        for axis in (
            "target_metric",
            "scope",
            "time_window",
            "baseline",
            "baseline_candidates",
        ):
            self.assertNotIn(axis, request)

    def test_prior_topic_material_digest_tamper_fails_before_provider(self):
        from tests.phase7.test_conversation_runtime import _seed_runtime

        turn = _seed_runtime().handle_message(
            "thread-phase7",
            "继续看刚才的渠道贡献。",
        )
        base_request = turn.run_request.to_dict()
        variants = {
            "digest": lambda request: request[
                "prior_topic_material_context"
            ].__setitem__("context_digest", "tampered"),
        }
        for axis, mutate in variants.items():
            with self.subTest(axis=axis):
                request = deepcopy(base_request)
                mutate(request)
                llm = ScriptedLLMClient({})
                state = {
                    "request": request,
                    "llm_client": llm,
                    "llm_calls": [],
                    "checkpoint_events": [],
                }

                with self.assertRaisesRegex(
                    WorkflowFailure,
                    "prior_topic_material_context_",
                ):
                    _understand_business_intent(state)

                self.assertEqual(llm.calls, [])

    def test_prior_topic_non_mapping_authority_fails_as_contract_before_provider(self):
        from tests.phase7.test_conversation_runtime import _seed_runtime

        turn = _seed_runtime().handle_message(
            "thread-phase7",
            "继续看刚才的渠道贡献。",
        )
        request = turn.run_request.to_dict()
        request["prior_topic_material_context"]["authorities"] = [None]
        llm = ScriptedLLMClient({})
        state = {
            "request": request,
            "llm_client": llm,
            "llm_calls": [],
            "checkpoint_events": [],
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            (
                "^prior_topic_material_context_invalid:"
                "prior_topic_completed_authority_shape_invalid$"
            ),
        ) as raised:
            _understand_business_intent(state)

        self.assertEqual(raised.exception.failure_type, "contract")
        self.assertEqual(llm.calls, [])

    def test_private_prior_material_rejects_every_top_level_material_axis_before_provider(self):
        from tests.phase7.test_conversation_runtime import _seed_runtime

        turn = _seed_runtime().handle_message(
            "thread-phase7",
            "继续看刚才的渠道贡献。",
        )
        base_request = turn.run_request.to_dict()
        axis_values = {
            "question_family": "custom_baseline_comparison",
            "pattern_family": "weekly",
            "pattern_params": {"weekdays": ["monday"]},
            "target_claim": "comparative_change",
            "target": {"date": "2026-06-02"},
            "target_metric": "paid_amount",
            "scope": "full_sample",
            "time_window": {"target": "yesterday"},
            "baseline": "previous_day",
            "baseline_candidates": ["previous_day"],
        }

        for axis, value in axis_values.items():
            with self.subTest(axis=axis):
                request = deepcopy(base_request)
                request[axis] = value
                llm = ScriptedLLMClient({})
                state = {
                    "request": request,
                    "llm_client": llm,
                    "llm_calls": [],
                    "checkpoint_events": [],
                }

                with self.assertRaisesRegex(
                    WorkflowFailure,
                    (
                        "^prior_topic_material_context_request_axis_conflict:"
                        f"{axis}$"
                    ),
                ) as raised:
                    _understand_business_intent(state)

                self.assertEqual(raised.exception.failure_type, "contract")
                self.assertEqual(llm.calls, [])

    def test_topic_summary_does_not_create_prior_business_material_fallback(self):
        payload = workflow_module._business_intent_payload(
            {
                "question": "继续分析",
                "context_manifest": {
                    "items": [
                        {
                            "source_type": "topic",
                            "summary": "上一轮比较前一天与七日均值。",
                        }
                    ]
                },
            }
        )

        self.assertNotIn("bound_business_context", payload)

    def test_two_turn_followup_preserves_reversed_prior_baseline_order_for_provider(self):
        from bi_agent.conversation.runtime import ConversationRuntime
        from bi_agent.conversation.store import InMemoryConversationStore
        from tests.phase7.test_conversation_runtime import (
            _add_authoritative_result_candidate,
        )

        store = InMemoryConversationStore()
        store.create_thread("thread-reversed-baselines", owner_id="analyst-1")
        topic = store.create_topic(
            "thread-reversed-baselines",
            title="付费金额基线比较",
            summary="上一轮完成了多基线比较。",
        )
        store.set_current_topic("thread-reversed-baselines", topic.topic_id)
        _add_authoritative_result_candidate(
            store,
            topic_id=topic.topic_id,
            result_ref="result:reversed-baselines",
            source_run_id="run-reversed-baselines",
            baselines=("same_weekday_last_week", "previous_day"),
        )
        second_turn = ConversationRuntime(store).handle_message(
            "thread-reversed-baselines",
            "继续分析这个结论。",
        )

        class CapturingIntentLLM(ScriptedLLMClient):
            def __init__(self):
                super().__init__(
                    {
                        "business_intent": _provider_business_intent_output(
                            question_family="custom_baseline_comparison",
                            pattern_family="custom_baseline",
                            pattern_params={
                                "period_key": "period",
                                "group_key": "group",
                                "target_group": "target",
                                "baseline_group": "baseline",
                            },
                            target_claim="comparative_change",
                            baseline_candidates=[
                                "same_weekday_last_week",
                                "previous_day",
                            ],
                        )
                    }
                )
                self.messages = []

            def invoke_json(
                self,
                *,
                task,
                prompt_version,
                messages,
                required_keys,
            ):
                self.messages = [dict(message) for message in messages]
                return super().invoke_json(
                    task=task,
                    prompt_version=prompt_version,
                    messages=messages,
                    required_keys=required_keys,
                )

        llm = CapturingIntentLLM()
        state = {
            "request": second_turn.run_request.to_dict(),
            "llm_client": llm,
            "llm_calls": [],
            "checkpoint_events": [],
        }

        _understand_business_intent(state)

        user_prompt = next(
            message["content"]
            for message in llm.messages
            if message["role"] == "user"
        )
        prompt_payload = json.loads(
            user_prompt.split("<input_json>", 1)[1]
            .split("</input_json>", 1)[0]
            .strip()
        )
        self.assertEqual(
            prompt_payload["bound_business_context"]["prior_baselines"],
            ["same_weekday_last_week", "previous_day"],
        )
        self.assertEqual(
            second_turn.run_request.to_dict()["prior_topic_material_context"]
            ["material_projection"]["intent_material"]["baselines"],
            ["same_weekday_last_week", "previous_day"],
        )

    def test_production_business_intent_rejects_scope_outside_reviewed_vocabulary(self):
        output = {
            "question_family": "revenue_health_review",
            "target_metric": "paid_amount",
            "pattern_family": "intra_period",
            "pattern_params": {"target_phase": "target_day"},
            "scope": "unreviewed_population_scope",
            "time_window": "yesterday",
            "target_claim": "comparative_change",
            "baseline_candidates": ["previous_day"],
            "analysis_requirements": {
                "context_sources": [],
                "claim_intents": ["comparative_change"],
                "requested_dimensions": [],
                "requested_components": [],
            },
            "answer_contract": {},
        }
        state = {
            "request": {
                "question": "检查指定总体范围的经营表现。",
                "run_mode": "production",
            },
            "llm_client": ScriptedLLMClient({"business_intent": output}),
            "llm_calls": [],
            "checkpoint_events": [],
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            "business_intent_contract_invalid:scope",
        ):
            _understand_business_intent(state)

    def test_business_intent_rejects_wrong_advisory_sequence_containers(self):
        for field, raw in (
            ("sub_intents", {"intent": "compare"}),
            ("sub_intents", "compare"),
            ("ambiguous_slots", {"slot": "baseline"}),
            ("ambiguous_slots", b"baseline"),
        ):
            with self.subTest(field=field, raw=raw):
                state = {
                    "request": {"question": "检查业务变化"},
                    "llm_client": ScriptedLLMClient({
                        "business_intent": _provider_business_intent_output(
                            question_family="pattern_explanation",
                            pattern_family="intra_period",
                            pattern_params={"target_phase": "target_day"},
                            **{field: raw},
                        )
                    }),
                    "llm_calls": [],
                    "checkpoint_events": [],
                }

                with self.assertRaisesRegex(
                    WorkflowFailure,
                    f"business_intent_contract_invalid:{field}",
                ):
                    _understand_business_intent(state)

    def test_available_evidence_brief_projects_only_verified_authority_and_scoped_gaps(self):
        gaps = diagnose_contract_gaps(
            contract_gaps=({
                "gap_id": "gap:paid:1",
                "fields": ("payment_attempt",),
            },),
            available_fields=(),
            contract_fields=(),
            restricted_output_fields=(),
            unsupported_grains=(),
        )
        brief = build_available_evidence_brief(
            verified_claims=(
                {
                    "claim_ref": "claim:market:1",
                    "text": "大盘付费金额下降 8%。",
                    "evidence_refs": ["evidence:market:1"],
                    "result_refs": ["result:market:1"],
                    "artifact_refs": ["artifact:market:1"],
                    "memory_refs": ["memory:market:1"],
                    "context_manifest_ref": "context:market:1",
                    "reuse_decisions": [{"source_ref": "source:market:1", "result_ref": "result:market:1", "decision": "reuse"}],
                    "provenance_record_ref": "trusted:market:1",
                },
                {
                    "claim_ref": "claim:untrusted:1",
                    "text": "untrusted",
                    "evidence_refs": [],
                },
            ),
            capability_bindings=(
                {"capability_id": "market_compare", "status": "ready"},
                {"capability_id": "paid_driver", "status": "degraded"},
                {"capability_id": "event_evidence", "status": "blocked"},
            ),
            contract_gaps=gaps,
            obligation_resolution={"unresolved": ["event_evidence_required"]},
        )

        self.assertEqual(
            [claim["claim_ref"] for claim in brief["verified_claims"]],
            ["claim:market:1"],
        )
        self.assertEqual(
            brief["verified_capabilities"],
            ["market_compare", "paid_driver"],
        )
        self.assertEqual(brief["omitted_factors"], ["gap:paid:1"])
        self.assertEqual(
            brief["business_next_actions"],
            [gaps[0]["repair_path"]],
        )
        self.assertEqual(
            brief["unresolved_obligations"], ["event_evidence_required"]
        )

    def test_persisted_query_gap_keeps_waiting_terminal_status(self):
        compiled = compile_graph(
            question_family="custom_baseline_comparison",
            target_metric="paid_amount",
            requested_nodes=("compare_periods",),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "run_id": "query-gap-waiting-status",
                "request": {"artifact_root": tmpdir},
                "compiled_graph": compiled,
                "analysis_route": {"requested_nodes": ["compare_periods"]},
                "query_gap_clarification": {
                    "questions": [{"question": "确认口径", "options": ["按推荐继续"]}]
                },
            }
            result = _persist_query_gap_clarification(state)

        self.assertEqual(result["workflow_status"], "waiting_for_clarification")
        self.assertEqual(
            result["answer_package"]["status"],
            "waiting_for_clarification",
        )

    def test_analysis_runtime_request_binds_all_fixed_eval_windows(self):
        request = _analysis_runtime_request({
            "run_id": "fixed-window-request",
            "request": {
                "run_mode": "production",
                "analysis_context": {
                    "as_of": "2026-06-03T12:00:00+01:00",
                    "target_date": "2026-06-02",
                    "previous_day": "2026-06-01",
                    "rolling_7_day_start": "2026-05-26",
                    "rolling_7_day_end": "2026-06-01",
                    "same_weekday_last_week": "2026-05-26",
                    "pattern_history_start": "2026-01-01",
                    "anomaly_history_start": "2026-05-03",
                },
            },
            "intent": {"question_family": "paid_amount_change_explanation"},
            "analysis_route": {
                "requested_nodes": ["pattern_scan", "outlier_scan"],
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "baselines": [
                        "前日付费金额",
                        "rolling_7d_avg",
                        "same_day_last_week",
                    ],
                },
            },
        })

        self.assertEqual(
            request.proposal["fixed_window_bounds"],
            {
                "target_day": ("2026-06-02", "2026-06-02"),
                "previous_day": ("2026-06-01", "2026-06-01"),
                "rolling_7_day_baseline": ("2026-05-26", "2026-06-01"),
                "same_weekday_last_week": ("2026-05-26", "2026-05-26"),
                "pattern_history": ("2026-01-01", "2026-06-02"),
                "anomaly_history": ("2026-05-03", "2026-06-01"),
            },
        )
        self.assertEqual(
            request.proposal["baselines"],
            (
                "previous_day",
                "rolling_7_day_baseline",
                "same_weekday_last_week",
            ),
        )


    def test_missing_run_mode_uses_production_material_validation_from_first_node(self):
        result = _run_pattern_workflow(
            {
                "run_id": "missing-mode-production-material-contract",
                "question": "检查昨天的付费变化",
                "llm_client": ScriptedLLMClient(
                    {
                        "business_intent": _provider_business_intent_output(
                            target_metric="",
                        )
                    }
                ),
            }
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.failure_reason,
            "business_intent_contract_invalid:target_metric",
        )
        self.assertEqual(
            [event["node"] for event in result.checkpoint_events],
            ["understand_business_intent"],
        )

    def test_workflow_entry_rejects_blank_or_unknown_run_mode(self):
        for run_mode in ("", "unknown", None, [], {}):
            with self.subTest(run_mode=run_mode):
                result = _run_pattern_workflow(
                    {
                        "run_id": "invalid-run-mode",
                        "run_mode": run_mode,
                        "llm_client": ScriptedLLMClient({}),
                    }
                )

                self.assertEqual(result.status, "failed")
                self.assertEqual(
                    result.failure_reason,
                    "analysis_runtime_run_mode_invalid",
                )
                self.assertEqual(result.checkpoint_events, ())


    def test_analysis_route_prompt_separates_context_from_metric_dataset_authority(self):
        text = "\n".join(
            message["content"]
            for message in build_prompt(
                "analysis_route_plan",
                {
                    "allowed_dataset_ids": ["paid_order_success", "gameplay"],
                    "allowed_context_source_ids": ["gameplay"],
                },
            ).messages
        )

        self.assertIn(
            "context_sources may use only allowed_context_source_ids",
            text,
        )
        self.assertIn("an empty array is valid", text)
        self.assertIn("dataset_requirements", text)
        self.assertIn("Metric-only datasets", text)
        self.assertNotIn(
            "context_sources must come from allowed_dataset_ids",
            text,
        )

    def test_route_card_claim_types_match_runtime_contract(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        cards = {
            card["capability_id"]: card
            for card in workflow_module._route_capability_cards()
        }

        self.assertEqual(
            cards["joint_attribution"]["allowed_claim_types"],
            registry.capability_inputs("joint_attribution")[
                "supported_claim_types"
            ],
        )

    def test_query_gap_clarification_prompt_has_business_options_and_escape(self):
        prompt = build_prompt(
            "query_gap_clarification",
            {"business_gaps": [{"business_gap": "业务时间范围不可用"}]},
        )
        text = "\n".join(message["content"] for message in prompt.messages)

        self.assertEqual(
            prompt.required_keys,
            (
                "questions",
                "recommended_assumption",
                "recommendation_reason",
                "decision_summary",
                "display_summary",
            ),
        )
        self.assertIn("exactly one question", text)
        self.assertIn("allowed_business_options", text)
        self.assertIn("do not author or repeat an options array", text)
        self.assertIn("WAJE renders the reviewed business options", text)
        self.assertIn("questions must never be empty", text)
        self.assertIn("tell the agent to do differently", text)
        self.assertIn("cannot claim", text)
        self.assertIn("character-for-character", text)
        self.assertIn("allowed_business_options", text)
        self.assertIn("non-empty recommendation_reason", text)
        self.assertNotIn("allowed_business_options. return.", text)
        self.assertIn("future availability timestamp", text)

    def test_all_clarification_prompts_treat_reviewed_escape_as_exact_contract_token(self):
        escape = "tell the agent to do differently"
        payloads = {
            "boundary_decision": {
                "intent": {"target_metric": "paid_amount"},
            },
            "clarification_question": {
                "boundary_decision": {"boundary_status": "needs_question"},
            },
            "query_gap_clarification": {
                "allowed_business_options": [
                    "继续可验证的主指标分析，并明确缺少相关业务背景证据",
                    "等待相关业务数据可用后再恢复本次分析",
                ],
            },
        }

        for task, payload in payloads.items():
            with self.subTest(task=task):
                system_message, task_message = build_prompt(task, payload).messages
                self.assertIn(
                    "reviewed clarification escape option is a machine contract token",
                    system_message["content"],
                )
                self.assertIn(
                    '"reviewed_clarification_escape_option": '
                    f'"{escape}"',
                    task_message["content"],
                )
                if task == "query_gap_clarification":
                    self.assertIn(
                        "do not author or repeat an options array",
                        task_message["content"],
                    )
                else:
                    self.assertIn(
                        "never translate or paraphrase the reviewed clarification escape option",
                        task_message["content"],
                    )

    def test_boundary_decision_needs_question_requires_one_exact_clarification(self):
        escape = "tell the agent to do differently"
        business_options = ["保留当前口径", "调整业务口径"]

        def state(output):
            return {
                "request": {},
                "intent": {
                    "scope": "full_sample",
                    "time_window": "target_day",
                    "pattern_family": "intra_period",
                },
                "llm_client": ScriptedLLMClient({"boundary_decision": output}),
                "llm_calls": [],
                "checkpoint_events": [],
            }

        valid = {
            "boundary_status": "needs_question",
            "recommended_assumption": {"option": business_options[0]},
            "clarification_questions": [{
                "question": "按哪个业务口径继续？",
                "options": [*business_options, escape],
            }],
            "decision_summary": "该选择会改变业务结论。",
        }
        valid_state = state(valid)
        _decide_question_boundary(valid_state)
        self.assertEqual(
            valid_state["boundary_decision"]["clarification_questions"],
            valid["clarification_questions"],
        )
        self.assertEqual(
            valid_state["boundary_decision"]["recommended_assumption"],
            valid["recommended_assumption"],
        )

        invalid_options = (
            [business_options[0], escape],
            [*business_options, "第三个业务口径", "第四个业务口径", escape],
            [*business_options, "按其他方式处理"],
            [*business_options, "Tell the agent to do differently"],
            [*business_options, f" {escape} "],
            [escape, *business_options],
            ["保留 evidence_ref 口径", business_options[1], escape],
        )
        for options in invalid_options:
            candidate = {
                **valid,
                "clarification_questions": [{
                    "question": "按哪个业务口径继续？",
                    "options": options,
                }],
            }
            with self.subTest(options=options), self.assertRaisesRegex(
                WorkflowFailure,
                "boundary_decision_contract_invalid",
            ):
                _decide_question_boundary(state(candidate))

        multiple_questions = {
            **valid,
            "clarification_questions": [
                *valid["clarification_questions"],
                {
                    "question": "还要确认时间口径吗？",
                    "options": [*business_options, escape],
                },
            ],
        }
        with self.assertRaisesRegex(
            WorkflowFailure,
            "boundary_decision_contract_invalid",
        ):
            _decide_question_boundary(state(multiple_questions))

        invalid_recommendation = {
            **valid,
            "recommended_assumption": {"option": "使用未审核的默认口径"},
        }
        with self.assertRaisesRegex(
            WorkflowFailure,
            "boundary_decision_contract_invalid:recommended_option",
        ):
            _decide_question_boundary(state(invalid_recommendation))

    def test_boundary_decision_nonquestion_status_requires_empty_questions(self):
        def state(status, questions):
            return {
                "request": {},
                "intent": {
                    "scope": "full_sample",
                    "time_window": "target_day",
                    "pattern_family": "intra_period",
                },
                "llm_client": ScriptedLLMClient({
                    "boundary_decision": {
                        "boundary_status": status,
                        "recommended_assumption": {},
                        "clarification_questions": questions,
                        "decision_summary": "已确认业务边界。",
                    }
                }),
                "llm_calls": [],
                "checkpoint_events": [],
            }

        for status in ("clear", "low_risk_assumption", "cannot_answer"):
            valid = state(status, [])
            if status == "low_risk_assumption":
                valid["llm_client"] = ScriptedLLMClient({
                    "boundary_decision": {
                        "boundary_status": status,
                        "recommended_assumption": {
                            "option": "沿用当前业务口径继续"
                        },
                        "clarification_questions": [],
                        "decision_summary": "已确认业务边界。",
                    }
                })
            _decide_question_boundary(valid)
            self.assertEqual(
                valid["boundary_decision"]["clarification_questions"],
                [],
            )

            with self.subTest(status=status), self.assertRaisesRegex(
                WorkflowFailure,
                "boundary_decision_contract_invalid:questions",
            ):
                _decide_question_boundary(state(status, [{"question": "不应出现"}]))

    def test_final_llm_audit_material_finding_is_advisory(self):
        audit = normalize_final_answer_audit(
            {
                "material_findings": [
                    {
                        "code": "unsupported_material_claim",
                        "answer_excerpt": "活动直接导致上涨",
                        "context_anchor": {
                            "kind": "boundary",
                            "key": "原因边界",
                        },
                        "edit_action": "weaken",
                        "explanation": "现有证据不能确认该原因。",
                    }
                ],
                "display_summary": "发现一处需要弱化的表达。",
            }
        )

        self.assertFalse(audit["blocks_display"])
        self.assertEqual(audit["hard_blockers"], [])
        self.assertEqual(audit["risk_flags"], [])
        self.assertEqual(
            audit["repairable_warnings"],
            ["unsupported_material_claim"],
        )
        self.assertEqual(audit["display_status"], "ready_with_warnings")
        self.assertIn("已定位表达", audit["retry_instruction"])

    def test_query_gap_clarification_contract_failure_has_no_node_retry(self):
        class InvalidThenValidLLM(ScriptedLLMClient):
            def __init__(self):
                super().__init__(
                    {
                        "query_gap_clarification": (
                            _provider_query_gap_clarification_output(
                                [
                                    "继续可验证的主指标分析，并明确缺少相关业务背景证据",
                                    "等待相关业务数据可用后再恢复本次分析",
                                ]
                            )
                        )
                    }
                )
                self.message_batches = []

            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                self.message_batches.append([dict(message) for message in messages])
                if len(self.message_batches) == 1:
                    return ScriptedLLMResult(
                        {
                            "questions": [{
                                "question": "external_event 数据集最早 2026-06-09 可用，怎么处理？",
                                "options": [
                                    "等待未来快照。",
                                    "tell the agent to do differently",
                                ],
                            }],
                            "recommended_assumption": {"option": "等待未来快照。"},
                            "decision_summary": "",
                            "display_summary": "",
                        },
                        {"task": task},
                    )
                return super().invoke_json(
                    task=task,
                    prompt_version=prompt_version,
                    messages=messages,
                    required_keys=required_keys,
                )

        fake = InvalidThenValidLLM()
        state = {
            "request": {},
            "analysis_runtime_result": type(
                "ClarificationResult",
                (),
                {"typed_gaps": ({
                    "gap_type": "dataset_snapshot_unavailable_as_of",
                    "requires_clarification": True,
                    "dataset_id": "external_event",
                    "diagnostic_context": {
                        "earliest_loaded_at": "2026-06-09T00:00:00+00:00",
                    },
                },)},
            )(),
            "query_repair_decisions": [],
            "intent": {"target_metric": "active_users", "time_window": "previous_day"},
            "llm_client": fake,
            "llm_calls": [],
            "checkpoint_events": [],
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            "query_gap_clarification_internal_authority_leak",
        ):
            _retrying_node(
                "generate_query_gap_clarification",
                _generate_query_gap_clarification,
            )(state)

        self.assertEqual(len(fake.message_batches), 1)
        first_prompt = "\n".join(
            message["content"] for message in fake.message_batches[0]
        )
        for hidden_value in (
            "external_event",
            "2026-06-09",
            "dataset_snapshot_unavailable_as_of",
        ):
            self.assertNotIn(hidden_value, first_prompt)
        self.assertIn("业务数据在分析时点尚不可用", first_prompt)

    def test_query_gap_business_projection_excludes_authority_metadata(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        projected = _business_query_gap_projection(
            ({
                "gap_type": "dataset_snapshot_unavailable_as_of",
                "requires_clarification": True,
                "gap_id": "dataset:external_event:dataset_snapshot_unavailable_as_of",
                "dataset_id": "external_event",
                "owner": "data_owner",
                "affected_capabilities": ("event_evidence",),
                "repair_options": (
                    "use_historical_snapshot_loaded_by_as_of",
                    "wait_for_snapshot_availability",
                ),
                "diagnostic_context": {
                    "as_of": "2026-06-03T11:00:00+00:00",
                    "earliest_snapshot_ref": "snapshot:event:future",
                    "earliest_loaded_at": "2026-06-09T00:00:00+00:00",
                },
            }, {
                "gap_type": "source_unbound",
                "owner": "data_owner",
                "affected_capabilities": ("event_evidence",),
                "repair_options": ("bind_source",),
                "requires_clarification": False,
            }),
            {"target_metric": "活跃用户", "time_window": "目标日与前一日"},
            accepted_capabilities=("market_health_compare", "event_evidence"),
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )

        rendered = json.dumps(projected, ensure_ascii=False)
        self.assertIn("业务数据在分析时点尚不可用", rendered)
        self.assertIn("等待相关业务数据可用后再恢复本次分析", rendered)
        self.assertIn("继续可验证的主指标分析，并明确缺少相关业务背景证据", rendered)
        self.assertNotIn("使用分析时点内已有的历史业务范围", rendered)
        self.assertNotIn("绑定业务来源", rendered)
        for hidden_value in (
            "external_event",
            "snapshot:event:future",
            "2026-06-09",
            "dataset_snapshot_unavailable_as_of",
        ):
            self.assertNotIn(hidden_value, rendered)

    def test_query_gap_clarification_binds_exact_actions_in_one_llm_call(self):
        def state(llm):
            return {
                "request": {},
                "analysis_route": {
                    "requested_nodes": ["market_health_compare", "event_evidence"]
                },
                "analysis_runtime_result": type(
                    "ClarificationResult",
                    (),
                    {"typed_gaps": ({
                        "gap_type": "dataset_snapshot_unavailable_as_of",
                        "requires_clarification": True,
                        "dataset_id": "external_event",
                        "owner": "data_owner",
                        "affected_capabilities": ("event_evidence",),
                        "repair_options": (
                            "use_historical_snapshot_loaded_by_as_of",
                            "wait_for_snapshot_availability",
                        ),
                        "diagnostic_context": {
                            "earliest_loaded_at": "2026-06-09T00:00:00+00:00"
                        },
                    },)},
                )(),
                "query_repair_decisions": [],
                "intent": {"target_metric": "active_users", "time_window": "previous_day"},
                "llm_client": llm,
                "llm_calls": [],
                "checkpoint_events": [],
            }

        options = [
            "继续可验证的主指标分析，并明确缺少相关业务背景证据",
            "等待相关业务数据可用后再恢复本次分析",
        ]
        fake = ScriptedLLMClient(
            {
                "query_gap_clarification": (
                    _provider_query_gap_clarification_output(
                        options,
                        recommended=options[0],
                    )
                )
            }
        )
        result = _generate_query_gap_clarification(state(fake))
        self.assertEqual(
            result["query_gap_clarification"]["recommended_assumption"],
            {"option": "继续可验证的主指标分析，并明确缺少相关业务背景证据（推荐）"},
        )
        self.assertEqual(fake.calls, ["query_gap_clarification"])
        actions = result["query_gap_clarification"]["choice_actions"]
        self.assertTrue(all(item.get("choice_id") for item in actions))
        self.assertTrue(actions[0]["business_reason"])
        self.assertEqual(
            [item["action_kind"] for item in actions],
            ["omit_unavailable_context", "wait_for_source", "user_redirect"],
        )

    def test_ready_independent_capability_forces_omit_recommendation_across_multiple_gaps(self):
        options = [
            "继续可验证的主指标分析，并明确缺少相关业务背景证据",
            "等待相关业务数据可用后再恢复本次分析",
        ]
        fake = ScriptedLLMClient(
            {
                "query_gap_clarification": (
                    _provider_query_gap_clarification_output(
                        options,
                        recommended=options[0],
                    )
                )
            }
        )

        def state():
            return {
                "request": {},
                "analysis_route": {
                    "requested_nodes": [
                        "market_health_compare",
                        "event_evidence",
                        "gameplay_context",
                    ]
                },
                "analysis_runtime_result": type(
                    "ClarificationResult",
                    (),
                    {
                        "typed_gaps": ({
                            "gap_type": "dataset_snapshot_unavailable_as_of",
                            "requires_clarification": True,
                            "owner": "data_owner",
                            "affected_capabilities": ("event_evidence",),
                            "repair_options": ("wait_for_snapshot_availability",),
                        }, {
                            "gap_type": "contract_partial",
                            "requires_clarification": True,
                            "owner": "contract_owner",
                            "affected_capabilities": ("gameplay_context",),
                            "repair_options": ("bind_source",),
                        }),
                        "bound_capability_inputs": {
                            "market_health_compare": _claim_ready_bound(
                                "binding:market"
                            ),
                            "event_evidence": type("Bound", (), {"status": "blocked"})(),
                            "gameplay_context": type("Bound", (), {"status": "degraded"})(),
                        },
                    },
                )(),
                "query_repair_decisions": [],
                "intent": {"target_metric": "active_users", "time_window": "previous_day"},
                "llm_client": fake,
                "llm_calls": [],
                "checkpoint_events": [],
            }

        result = _generate_query_gap_clarification(state())
        clarification = result["query_gap_clarification"]
        recommended = clarification["recommended_assumption"]["option"]
        action_by_label = {
            item["business_label"]: item["action_kind"]
            for item in clarification["choice_actions"]
        }
        self.assertEqual(action_by_label[recommended], "omit_unavailable_context")
        self.assertEqual(fake.calls, ["query_gap_clarification"])

    def test_boundary_only_acceptance_is_scoped_to_gaps_when_ready_sibling_exists(self):
        result = type(
            "ClarificationResult",
            (),
            {
                "status": "clarify",
                "typed_gaps": ({
                    "gap_type": "dataset_snapshot_unavailable_as_of",
                    "requires_clarification": True,
                    "affected_capabilities": ("event_evidence",),
                },),
                "bound_capability_inputs": {
                    "market_health_compare": _claim_ready_bound("binding:market"),
                    "event_evidence": type("Bound", (), {"status": "blocked"})(),
                },
            },
        )()
        state = {
            "request": {
                "accepted_degradation_choice": {
                    "choice_id": "continue-with-boundary",
                    "action_kind": "continue_with_boundary_only",
                    "affected_capabilities": ["event_evidence"],
                }
            },
            "analysis_runtime_result": result,
        }

        self.assertEqual(_route_after_query_repair(state), "degraded")
        self.assertEqual(
            state["request"]["accepted_degradation_choice"]["action_kind"],
            "omit_unavailable_context",
        )

    def test_global_gap_cannot_prove_ready_sibling_independence(self):
        result = type(
            "ClarificationResult",
            (),
            {
                "status": "clarify",
                "typed_gaps": ({
                    "gap_type": "contract_partial",
                    "requires_clarification": True,
                    "affected_capabilities": (),
                },),
                "bound_capability_inputs": {
                    "market_health_compare": _claim_ready_bound("binding:market"),
                },
            },
        )()
        state = {
            "request": {
                "accepted_degradation_choice": {
                    "action_kind": "continue_with_boundary_only",
                }
            },
            "analysis_runtime_result": result,
        }

        self.assertEqual(_route_after_query_repair(state), "degraded")
        self.assertEqual(
            state["request"]["accepted_degradation_choice"]["action_kind"],
            "continue_with_boundary_only",
        )

    def test_explicit_wait_and_redirect_are_not_rewritten_for_ready_sibling(self):
        result = type(
            "ClarificationResult",
            (),
            {
                "status": "clarify",
                "typed_gaps": ({
                    "gap_type": "source_unbound",
                    "requires_clarification": True,
                    "affected_capabilities": ("event_evidence",),
                },),
                "bound_capability_inputs": {
                    "market_health_compare": _claim_ready_bound("binding:market"),
                },
            },
        )()
        for action in ("wait_for_source", "user_redirect"):
            state = {
                "request": {"accepted_degradation_choice": {"action_kind": action}},
                "analysis_runtime_result": result,
            }
            self.assertEqual(_route_after_query_repair(state), "clarify")
            self.assertEqual(
                state["request"]["accepted_degradation_choice"]["action_kind"],
                action,
            )

    def test_analysis_runtime_request_is_typed_and_requires_fixed_clock(self):
        request = AnalysisRuntimeRequest.create(
            run_id="run-runtime",
            proposal={
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
                "scope": {"type": "full_sample"},
                "target_semantic": "yesterday",
                "baselines": ["previous_day"],
            },
            accepted_graph=("compare_periods",),
            as_of="2026-06-03T12:00:00+01:00",
        )

        self.assertEqual(request.as_of.isoformat(), "2026-06-03T12:00:00+01:00")
        self.assertEqual(request.accepted_graph, ("compare_periods",))
        self.assertTrue(hasattr(AnalysisRuntime, "execute"))

    def test_route_reconciliation_carries_independent_capability_dataset(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        requested, reconciled = workflow_module.reconcile_analysis_route(
            ("market_health_compare",),
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "dataset_requirements": ["paid_order_success"],
                    "baselines": ["previous_day"],
                    "claim_intents": ["comparative_change"],
                }
            },
            {
                "question_family": "revenue_health_review",
                "question_families": ["revenue_health_review"],
                "target_metric": "paid_amount",
            },
            registry,
        )

        self.assertIn("market_health_compare", requested)
        self.assertEqual(
            reconciled["analysis_requirements"]["dataset_requirements"],
            ["paid_order_success", "market_dashboard"],
        )
        self.assertEqual(
            reconciled["obligation_resolution"][
                "capability_dataset_requirements"
            ]["market_health_compare"],
            ["market_dashboard"],
        )

    def test_auxiliary_time_axis_derives_context_window_without_promoting_route_selection(self):
        registry = workflow_module.RuntimeContractRegistry.from_path(
            workflow_module.CANONICAL_RUNTIME_BINDINGS_PATH
        )
        intent = {
            "question_family": "paid_amount_change_explanation",
            "question_families": ["paid_amount_change_explanation"],
            "target_metric": "paid_amount",
            "analysis_axes": [
                {
                    "axis_id": "time_context",
                    "role": "auxiliary",
                    "capability_refs": [
                        "metric_timeseries",
                        "rolling_window_compare",
                    ],
                    "explicit_focus_refs": {
                        "component_ids": [],
                        "dimension_ids": [],
                        "context_source_ids": [],
                    },
                }
            ],
        }

        requested, route = workflow_module.reconcile_analysis_route(
            ("rolling_window_compare",),
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "baselines": ["previous_day"],
                    "context_window_specs": [
                        {
                            "capability_id": "rolling_window_compare",
                            "relation": "trailing_complete_periods",
                            "unit": "day",
                            "count": 14,
                        }
                    ],
                    "diagnostic_tags": [],
                }
            },
            intent,
            registry,
        )

        self.assertIn("rolling_window_compare", requested)
        self.assertEqual(
            route["analysis_requirements"]["baselines"],
            ["previous_day"],
        )
        self.assertEqual(
            route["analysis_requirements"]["context_window_specs"],
            [
                {
                    "capability_id": "rolling_window_compare",
                    "relation": "trailing_complete_periods",
                    "unit": "day",
                    "count": 14,
                }
            ],
        )
        role = route["obligation_resolution"]["capability_roles"][
            "rolling_window_compare"
        ]
        self.assertEqual(role["analysis_role"], "auxiliary")
        self.assertIn("route_selected", role["sources"])
        self.assertIn("analysis_axis:time_context:auxiliary", role["sources"])
        self.assertNotIn(
            "rolling_window_compare",
            route["obligation_resolution"]["required_capabilities"],
        )

    def test_unselected_rolling_candidate_does_not_inject_context_into_period_route(self):
        registry = workflow_module.RuntimeContractRegistry.from_path(
            workflow_module.CANONICAL_RUNTIME_BINDINGS_PATH
        )
        requested, route = workflow_module.reconcile_analysis_route(
            ("compare_period_phases",),
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "baselines": ["previous_day"],
                    "diagnostic_tags": [],
                }
            },
            {
                "question_family": "pattern_explanation",
                "question_families": ["pattern_explanation"],
                "target_metric": "paid_amount",
                "pattern_family": "intra_period",
                "analysis_axes": [
                    {
                        "axis_id": "time_context",
                        "role": "auxiliary",
                        "capability_refs": [
                            "metric_timeseries",
                            "rolling_window_compare",
                        ],
                        "explicit_focus_refs": {
                            "component_ids": [],
                            "dimension_ids": [],
                            "context_source_ids": [],
                        },
                    }
                ],
            },
            registry,
        )

        self.assertIn("compare_period_phases", requested)
        self.assertNotIn("rolling_window_compare", requested)
        self.assertEqual(
            route["analysis_requirements"]["context_window_specs"], []
        )

    def test_period_route_preserves_selected_quarter_context_spec(self):
        registry = workflow_module.RuntimeContractRegistry.from_path(
            workflow_module.CANONICAL_RUNTIME_BINDINGS_PATH
        )
        spec = {
            "capability_id": "compare_period_phases",
            "relation": "trailing_complete_periods",
            "unit": "quarter",
            "count": 1,
        }

        requested, route = workflow_module.reconcile_analysis_route(
            ("compare_period_phases",),
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "baselines": ["previous_day"],
                    "context_window_specs": [spec],
                    "diagnostic_tags": [],
                }
            },
            {
                "question_family": "pattern_explanation",
                "question_families": ["pattern_explanation"],
                "target_metric": "paid_amount",
                "pattern_family": "intra_period",
                "analysis_axes": [],
            },
            registry,
        )

        self.assertIn("compare_period_phases", requested)
        self.assertNotIn("rolling_window_compare", requested)
        self.assertEqual(
            route["analysis_requirements"]["context_window_specs"],
            [spec],
        )

    def test_required_time_axis_upgrades_selected_context_capability(self):
        registry = workflow_module.RuntimeContractRegistry.from_path(
            workflow_module.CANONICAL_RUNTIME_BINDINGS_PATH
        )

        def reconcile(*, axis_role):
            return workflow_module.reconcile_analysis_route(
                ("rolling_window_compare",),
                {
                    "analysis_requirements": {
                        "target_metrics": ["paid_amount"],
                        "baselines": ["previous_day"],
                        "context_window_specs": [
                            {
                                "capability_id": "rolling_window_compare",
                                "relation": "trailing_complete_periods",
                                "unit": "day",
                                "count": 7,
                            }
                        ],
                        "diagnostic_tags": [],
                    }
                },
                {
                    "question_family": "paid_amount_change_explanation",
                    "question_families": ["paid_amount_change_explanation"],
                    "target_metric": "paid_amount",
                    "analysis_axes": [
                        {
                            "axis_id": "time_context",
                            "role": axis_role,
                            "capability_refs": ["rolling_window_compare"],
                            "explicit_focus_refs": {
                                "component_ids": [],
                                "dimension_ids": [],
                                "context_source_ids": [],
                            },
                        }
                    ],
                },
                registry,
            )[1]

        required_axis_route = reconcile(axis_role="required")
        required_axis_role = required_axis_route["obligation_resolution"][
            "capability_roles"
        ]["rolling_window_compare"]
        self.assertEqual(required_axis_role["analysis_role"], "required")
        self.assertIn(
            "analysis_axis:time_context:required",
            required_axis_role["sources"],
        )
        self.assertEqual(
            required_axis_route["analysis_requirements"]["context_window_specs"],
            [
                {
                    "capability_id": "rolling_window_compare",
                    "relation": "trailing_complete_periods",
                    "unit": "day",
                    "count": 7,
                }
            ],
        )

    def test_route_dataset_carry_normalizes_scalar_extra_and_is_idempotent(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        intent = {
            "question_family": "revenue_health_review",
            "question_families": ["revenue_health_review"],
            "target_metric": "paid_amount",
        }
        first = workflow_module.reconcile_analysis_route(
            ("market_health_compare",),
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "dataset_requirements": "paid_order_success",
                    "baselines": ["previous_day"],
                }
            },
            intent,
            registry,
        )
        second = workflow_module.reconcile_analysis_route(
            first[0],
            first[1],
            intent,
            registry,
            trusted_prior_route=first[1],
        )

        self.assertEqual(
            first[1]["analysis_requirements"]["dataset_requirements"],
            ["paid_order_success", "market_dashboard"],
        )
        self.assertEqual(
            second[1]["analysis_requirements"]["dataset_requirements"],
            first[1]["analysis_requirements"]["dataset_requirements"],
        )


    def test_reconciliation_records_only_actual_metric_context_and_obligation_additions(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        intent = {
            "question_family": "anomaly_or_black_swan_review",
            "question_families": ["anomaly_or_black_swan_review"],
            "target_metric": "active_users",
        }
        route = {
            "analysis_requirements": {
                "target_metrics": ["active_users"],
                "baselines": ["previous_day"],
                "context_sources": ["external_event"],
            }
        }

        first = workflow_module.reconcile_analysis_route((), route, intent, registry)
        mutations = first[1]["obligation_resolution"]["mutations"]

        self.assertEqual(
            [(item["capability"], item["reason"]) for item in mutations],
            [
                ("market_health_compare", "metric_coverage_required"),
                ("event_evidence", "context_coverage_required"),
                ("data_quality_profile", "obligation_required"),
                ("outlier_scan", "obligation_required"),
                ("change_point_scan", "obligation_independent"),
            ],
        )
        second = workflow_module.reconcile_analysis_route(
            first[0], first[1], intent, registry
        )
        self.assertEqual(second[0], first[0])
        self.assertEqual(second[1]["obligation_resolution"]["mutations"], [])

    def test_every_public_family_rejects_zero_support_diagnostic_and_keeps_base(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        tags = (
            "driver_focus", "change_explanation", "pattern_attribution",
            "event_impact", "revenue_health", "factor_topk", "anomaly",
            "multi_baseline", "evidence_quality",
        )
        for family in registry.question_family_ids:
            tag = next(
                candidate for candidate in tags
                if family not in registry.diagnostic_obligation(candidate)[
                    "supported_question_families"
                ]
            )
            requested, route = workflow_module.reconcile_analysis_route(
                ("data_quality_profile",),
                {"analysis_requirements": {"diagnostic_tags": [tag]}},
                {
                    "question_family": family,
                    "question_families": [family],
                    "target_metric": "paid_amount",
                },
                registry,
            )
            state = {"route_material_conflicts": (), "boundary_decision": {}}
            workflow_module._consume_obligation_route_conflict(state, route)
            with self.subTest(family=family, tag=tag):
                base_required = set(
                    registry.question_family_obligation(family)[
                        "required_capabilities"
                    ]
                )
                self.assertTrue(base_required.issubset(requested))
                self.assertEqual(route["obligation_resolution"]["status"], "resolved")
                self.assertIn(
                    {
                        "action": "rejected",
                        "capability": tag,
                        "reason": "diagnostic_question_family_incompatible",
                    },
                    route["obligation_resolution"]["mutations"],
                )
                self.assertEqual(state["boundary_decision"], {})

    def test_unknown_diagnostic_tag_is_rejected_before_obligation_resolution(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        requested, route = workflow_module.reconcile_analysis_route(
            ("data_quality_profile",),
            {"analysis_requirements": {"diagnostic_tags": ["model_invented_tag"]}},
            {
                "question_family": "data_quality_or_evidence_review",
                "question_families": ["data_quality_or_evidence_review"],
                "target_metric": "paid_amount",
            },
            registry,
        )
        state = {"route_material_conflicts": (), "boundary_decision": {}}
        workflow_module._consume_obligation_route_conflict(state, route)

        self.assertEqual(
            requested,
            (
                "data_quality_profile",
                "metric_coverage_profile",
                "answer_verify",
            ),
        )
        self.assertEqual(route["obligation_resolution"]["status"], "resolved")
        self.assertIn(
            {
                "action": "rejected",
                "capability": "model_invented_tag",
                "reason": "unknown_diagnostic_rejected",
            },
            route["obligation_resolution"]["mutations"],
        )
        self.assertEqual(state["boundary_decision"], {})



    def test_incompatible_diagnostic_is_rejected_without_executing_its_capability(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        requested, route = workflow_module.reconcile_analysis_route(
            ("data_quality_profile",),
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "requested_dimensions": ["channel"],
                    "diagnostic_tags": ["factor_topk"],
                }
            },
            {
                "question_family": "data_quality_or_evidence_review",
                "question_families": ["data_quality_or_evidence_review"],
                "target_metric": "paid_amount",
                "analysis_requirements": {
                    "claim_intents": ["formula_component_contribution"]
                },
            },
            registry,
        )

        self.assertTrue(
            {"data_quality_profile", "metric_coverage_profile", "answer_verify"}
            .issubset(requested)
        )
        self.assertTrue(
            {"segment_contribution", "joint_attribution"}.isdisjoint(requested)
        )
        self.assertEqual(route["obligation_resolution"]["status"], "resolved")
        self.assertEqual(
            route["analysis_requirements"]["diagnostic_tags"],
            [],
        )
        self.assertIn(
            {
                "action": "rejected",
                "capability": "factor_topk",
                "reason": "diagnostic_question_family_incompatible",
            },
            route["obligation_resolution"]["mutations"],
        )



    def test_route_repair_rejects_conflicting_signed_analysis_requirements(self):
        from types import SimpleNamespace

        signed_requirements = {
            "target_metrics": ["paid_amount"],
            "requested_components": [],
            "requested_dimensions": [],
            "baselines": [],
            "context_sources": [],
            "dataset_requirements": ["paid_order_success"],
            "diagnostic_tags": ["revenue_health"],
            "claim_intents": ["comparative_change"],
            "scope": "full_sample",
        }
        state = {
            "request": {},
            "intent": {
                "question_family": "revenue_health_review",
                "question_families": ["revenue_health_review"],
                "target_metric": "paid_amount",
                "requested_nodes": ["metric_coverage_profile"],
            },
            "analysis_route": {
                "requested_nodes": ["metric_coverage_profile"],
                "analysis_requirements": deepcopy(signed_requirements),
            },
            "compiled_graph": SimpleNamespace(
                mutations=SimpleNamespace(records=())
            ),
            "obligation_rejection_history": (),
            "repair_attempts": 0,
        }
        conflicting_repair = {
            "requested_nodes": ["metric_coverage_profile"],
            "analysis_requirements": {
                **signed_requirements,
                "target_metrics": ["active_users"],
            },
            "repair_summary": "调整分析路线。",
            "decision_summary": "按修复建议继续。",
            "display_summary": "正在修复分析路线。",
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
            return_value=conflicting_repair,
        ), self.assertRaisesRegex(
            WorkflowFailure,
            "analysis_route_repair_material_conflict:analysis_requirements",
        ):
            workflow_module._repair_analysis_route(state)

    def test_production_route_repair_finalizes_narrative_after_machine_degradation(self):
        from types import SimpleNamespace

        current_nodes = (
            "rolling_window_compare",
            "metric_timeseries",
            "data_quality_profile",
            "evidence_reduce",
            "answer_verify",
            "compare_periods",
            "compare_period_phases",
            "weekday_calendar_compare",
        )
        requirements = _provider_analysis_route_output()["analysis_requirements"]
        state = {
            **_provider_analysis_route_state(None),
            "request": {
                "run_mode": "production",
                "accepted_degradation_choice": {
                    "action_kind": "omit_unavailable_context",
                    "affected_capabilities": ["rolling_window_compare"],
                },
            },
            "analysis_route": {
                **_provider_analysis_route_output(
                    requested_nodes=list(current_nodes),
                    expected_evidence={
                        capability: "修复前的业务证据描述。"
                        for capability in current_nodes
                    },
                    analysis_requirements=deepcopy(requirements),
                ),
            },
            "compiled_graph": SimpleNamespace(
                mutations=SimpleNamespace(records=())
            ),
            "obligation_rejection_history": (),
            "repair_attempts": 0,
            "llm_calls": [],
        }
        calls = []

        def invoke(_state, task, payload, **kwargs):
            calls.append((task, deepcopy(payload)))
            if task == "route_repair":
                return {
                    "requested_nodes": list(current_nodes),
                    "repair_summary": "按合同反馈修复机器路线。",
                    "decision_summary": "保留可执行的证据路径。",
                    "display_summary": "已修复分析路线。",
                }
            self.assertEqual(task, "final_route_narrative")
            route_steps = payload["route_context"]["route_steps"]
            candidate = _provider_final_route_narrative_output(
                tuple(route_steps),
                route_summary="按修复后的最终业务路径继续核验。",
                decision_summary="最终路线已覆盖保留的证据义务。",
                display_summary="已形成修复后的业务分析路线。",
            )
            kwargs["output_validator"](candidate)
            return candidate

        with patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
            side_effect=invoke,
        ):
            workflow_module._repair_analysis_route(state)

        self.assertEqual(
            [task for task, _ in calls],
            ["route_repair", "final_route_narrative"],
        )
        self.assertNotIn("prior_route_narrative", calls[1][1])
        self.assertNotIn("removed_capability_business_labels", calls[1][1])
        self.assertNotIn("rolling_window_compare", state["analysis_route"]["requested_nodes"])
        serialized_narrative_input = json.dumps(
            calls[1][1], ensure_ascii=False, sort_keys=True
        )
        self.assertNotIn("rolling_window_compare", serialized_narrative_input)
        self.assertEqual(
            workflow_module._final_narrative_capability_refs(
                state["analysis_route"]["requested_nodes"]
            ),
            state["analysis_route"]["narrative_capability_refs"],
        )
        self.assertNotIn("final_narrative_capability_refs", calls[1][1])
        self.assertEqual(
            set(state["analysis_route"]["expected_evidence"]),
            set(state["analysis_route"]["requested_nodes"]),
        )

    def test_final_route_narrative_retries_non_exact_capability_sections(self):
        from bi_agent.runtime.runtime_contract_registry import (
            RuntimeContractRegistry,
        )

        requested = ("compare_periods",)
        requirements = _provider_analysis_route_output()["analysis_requirements"]
        missing = _provider_final_route_narrative_output(
            requested,
            sections=[],
        )
        extra = _provider_final_route_narrative_output(
            requested,
            sections=[
                {
                    "step_ref": "step_1",
                    "route_step": "核对目标周期与基准周期的变化。",
                    "expected_evidence": "目标周期与基准周期之间的变化证据。",
                },
                {
                    "step_ref": "step_2",
                    "route_step": "补充未经安排的业务路径。",
                    "expected_evidence": "补充未经安排的业务证据。",
                },
            ],
        )
        valid = _provider_final_route_narrative_output(
            requested,
            route_summary="使用周期对比核对目标周期的业务变化。",
            decision_summary="保留周期对比证据路径。",
            display_summary="已形成周期对比路线。",
            sections=[
                {
                    "step_ref": "step_1",
                    "route_step": "核对目标周期与基准周期的变化。",
                    "expected_evidence": "目标周期与基准周期之间的变化证据。",
                }
            ],
        )
        client, completions = _provider_client_with_outputs(
            (missing, extra, valid)
        )
        state = _provider_analysis_route_state(client)
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )

        finalized = workflow_module._finalize_production_analysis_route_narrative(
            state,
            route={"analysis_requirements": deepcopy(requirements)},
            requested=requested,
            registry=registry,
        )

        self.assertEqual(completions.attempt_count, 3)
        self.assertEqual(
            set(finalized["capability_sections"]),
            set(requested),
        )
        self.assertIn(valid["route_summary"], finalized["route_summary"])
        self.assertIn(
            valid["sections"][0]["route_step"],
            finalized["route_summary"],
        )
        self.assertEqual(
            finalized["narrative_capability_refs"],
            workflow_module._final_narrative_capability_refs(requested),
        )
        self.assertEqual(
            finalized["narrative_authority"],
            workflow_module._final_narrative_authority(),
        )

    def test_cross_concept_route_section_cannot_expand_machine_authority(self):
        from bi_agent.runtime.runtime_contract_registry import (
            RuntimeContractRegistry,
        )

        requested = ("compare_periods",)
        requirements = {
            **deepcopy(_provider_analysis_route_output()["analysis_requirements"]),
            "claim_intents": ["baseline_stability"],
        }
        cross_concept_candidate = _provider_final_route_narrative_output(
            requested,
            route_summary="核对当前周期变化。",
            sections=[
                {
                    "step_ref": "step_1",
                    "route_step": "补充过去七天均值对照。",
                    "expected_evidence": "过去七天均值对照的业务说明。",
                }
            ],
        )
        clean_candidate = _provider_final_route_narrative_output(
            requested,
            route_summary="核对当前周期变化。",
            sections=[
                {
                    "step_ref": "step_1",
                    "route_step": "核对目标周期与基准周期的变化。",
                    "expected_evidence": "目标周期与基准周期之间的变化证据。",
                }
            ],
        )
        cross_client, cross_completions = _provider_client_with_outputs(
            (cross_concept_candidate,)
        )
        clean_client, _ = _provider_client_with_outputs((clean_candidate,))
        cross_state = _provider_analysis_route_state(cross_client)
        clean_state = _provider_analysis_route_state(clean_client)
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )

        finalized = workflow_module._finalize_production_analysis_route_narrative(
            cross_state,
            route={"analysis_requirements": deepcopy(requirements)},
            requested=requested,
            registry=registry,
        )
        clean_finalized = workflow_module._finalize_production_analysis_route_narrative(
            clean_state,
            route={"analysis_requirements": deepcopy(requirements)},
            requested=requested,
            registry=registry,
        )
        accepted_route = {**finalized, "requested_nodes": requested}
        clean_route = {**clean_finalized, "requested_nodes": requested}
        intent = {
            "question_family": "custom_baseline_comparison",
            "question_families": ["custom_baseline_comparison"],
            "primary_question_family": "custom_baseline_comparison",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
            "pattern_family": "custom_baseline",
            "scope": "full_sample",
            "requested_nodes": requested,
        }
        for target_state, route in (
            (cross_state, accepted_route),
            (clean_state, clean_route),
        ):
            target_state["request"] = {
                "run_mode": "production",
                "question": "检查目标周期变化",
            }
            target_state["intent"] = deepcopy(intent)
            target_state["analysis_route"] = route
            workflow_module._accept_analysis_route(target_state)

        self.assertEqual(cross_completions.attempt_count, 1)
        self.assertEqual(accepted_route["requested_nodes"], requested)
        self.assertEqual(set(accepted_route["expected_evidence"]), set(requested))
        self.assertNotIn(
            "rolling_window_compare",
            accepted_route["expected_evidence"],
        )
        self.assertEqual(
            accepted_route["analysis_requirements"]["claim_intents"],
            ["baseline_stability"],
        )
        self.assertEqual(
            accepted_route["narrative_authority"]["authority_level"],
            "display_advisory",
        )
        self.assertEqual(
            cross_state["compiled_graph"].mutations.accepted_graph,
            clean_state["compiled_graph"].mutations.accepted_graph,
        )
        self.assertEqual(
            cross_state["compiled_graph"].runtime_plan,
            clean_state["compiled_graph"].runtime_plan,
        )
        self.assertNotIn(
            "rolling_window_compare",
            cross_state["compiled_graph"].mutations.accepted_graph,
        )

    def test_production_route_accept_rejects_tampered_capability_sections(self):
        requested = ("compare_periods",)
        provider_route = _provider_analysis_route_output(
            requested_nodes=list(requested),
        )
        provider_narrative = _provider_final_route_narrative_output(requested)
        route = workflow_module._project_final_analysis_route_narrative(
            {
                "analysis_requirements": deepcopy(
                    provider_route["analysis_requirements"]
                )
            },
            provider_narrative,
            requested=requested,
        )
        route["requested_nodes"] = requested
        mutations = {
            "missing_section": (
                lambda value: value["capability_sections"].clear(),
                "capability_sections",
            ),
            "extra_section": (
                lambda value: value["capability_sections"].update(
                    {
                        "rolling_window_compare": {
                            "route_step": "补充滚动窗口路径。",
                            "expected_evidence": "补充滚动窗口证据。",
                        }
                    }
                ),
                "capability_sections",
            ),
            "extra_section_field": (
                lambda value: value["capability_sections"][
                    "compare_periods"
                ].update({"claim_contract": "伪造声明合同"}),
                "capability_sections",
            ),
            "wrong_section_type": (
                lambda value: value["capability_sections"].__setitem__(
                    "compare_periods", []
                ),
                "capability_sections",
            ),
            "tampered_refs": (
                lambda value: value["narrative_capability_refs"].update(
                    {"route_summary_capability_ids": []}
                ),
                "narrative_capability_refs",
            ),
            "tampered_authority": (
                lambda value: value["narrative_authority"].update(
                    {"authority_level": "hard"}
                ),
                "narrative_authority",
            ),
            "tampered_projection": (
                lambda value: value.__setitem__(
                    "route_summary", "替换持久化展示文本。"
                ),
                "route_summary_projection",
            ),
        }
        for boundary, (mutate, reason) in mutations.items():
            with self.subTest(boundary=boundary):
                tampered = deepcopy(route)
                mutate(tampered)
                state = {
                    "request": {
                        "run_mode": "production",
                        "question": "检查经营变化",
                    },
                    "intent": {
                        "question_family": "custom_baseline_comparison",
                        "question_families": ["custom_baseline_comparison"],
                        "target_metric": "paid_amount",
                        "pattern_family": "custom_baseline",
                        "requested_nodes": requested,
                    },
                    "analysis_route": tampered,
                }
                with self.assertRaisesRegex(
                    WorkflowFailure,
                    f"analysis_route_provider_contract_invalid:{reason}",
                ):
                    workflow_module._accept_analysis_route(state)


    def test_initial_route_only_supplies_required_ids_with_provider_visible_cards(self):
        state = _provider_analysis_route_state(None)
        calls = []

        def invoke(_state, task, payload, **kwargs):
            calls.append((task, deepcopy(payload)))
            if task == "analysis_route_plan":
                visible_ids = {
                    str(card.get("capability_id") or "")
                    for card in payload["known_capabilities"]
                }
                self.assertTrue(
                    set(payload["required_capability_ids"]).issubset(
                        visible_ids
                    )
                )
                candidate = _provider_analysis_route_output()
            else:
                self.assertEqual(task, "final_route_narrative")
                route_steps = payload["route_context"]["route_steps"]
                candidate = _provider_final_route_narrative_output(
                    tuple(route_steps),
                )
            kwargs["output_validator"](candidate)
            return candidate

        with patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
            side_effect=invoke,
        ):
            workflow_module._design_analysis_route(state)

        self.assertEqual(
            [task for task, _ in calls],
            ["analysis_route_plan", "final_route_narrative"],
        )

    def test_initial_and_graphless_attempt_routes_finalize_local_obligation_closure(self):
        for attempt_context in (
            {},
            {
                "analysis_route": _provider_analysis_route_output(),
                "accepted_graph": [],
            },
        ):
            with self.subTest(graphless_attempt=bool(attempt_context)):
                state = _provider_analysis_route_state(None)
                if attempt_context:
                    state["request"]["clarification_attempt_context"] = attempt_context
                calls = []

                def invoke(_state, task, payload, **kwargs):
                    calls.append((task, deepcopy(payload)))
                    if task == "analysis_route_plan":
                        candidate = _provider_analysis_route_output()
                    else:
                        self.assertEqual(task, "final_route_narrative")
                        route_steps = payload["route_context"]["route_steps"]
                        self.assertTrue(
                            any(
                                step["business_name"] == "指标时间序列"
                                for step in route_steps
                            )
                        )
                        candidate = _provider_final_route_narrative_output(
                            tuple(route_steps),
                        )
                    kwargs["output_validator"](candidate)
                    return candidate

                with patch(
                    "bi_agent.runtime.langgraph_workflow._invoke_llm",
                    side_effect=invoke,
                ):
                    workflow_module._design_analysis_route(state)

                self.assertEqual(
                    [task for task, _ in calls],
                    ["analysis_route_plan", "final_route_narrative"],
                )
                self.assertEqual(
                    set(state["analysis_route"]["capability_sections"]),
                    set(state["analysis_route"]["requested_nodes"]),
                )

    def test_final_route_narrative_retries_unknown_and_unsupplied_nodes(self):
        from bi_agent.runtime.runtime_contract_registry import (
            RuntimeContractRegistry,
        )

        requested = ("metric_timeseries",)
        requirements = _provider_analysis_route_output()["analysis_requirements"]
        unknown = _provider_final_route_narrative_output(
            requested,
            sections=[
                {
                    "step_ref": "invented_step",
                    "route_step": "核对未经供应的步骤。",
                    "expected_evidence": "获得未经供应的证据。",
                }
            ],
        )
        unsupplied = _provider_final_route_narrative_output(
            requested,
            sections=[
                {
                    "step_ref": "step_2",
                    "route_step": "核对错位步骤。",
                    "expected_evidence": "获得错位步骤证据。",
                }
            ],
        )
        valid = _provider_final_route_narrative_output(requested)
        client, completions = _provider_client_with_outputs(
            (unknown, unsupplied, valid)
        )
        state = _provider_analysis_route_state(client)
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )

        finalized = workflow_module._finalize_production_analysis_route_narrative(
            state,
            route={"analysis_requirements": deepcopy(requirements)},
            requested=requested,
            registry=registry,
        )

        self.assertEqual(completions.attempt_count, 3)
        self.assertEqual(tuple(finalized["capability_sections"]), requested)

    def test_final_route_narrative_exhaustion_keeps_machine_route_available(self):
        from bi_agent.runtime.runtime_contract_registry import (
            RuntimeContractRegistry,
        )

        requested = ("metric_timeseries",)
        requirements = _provider_analysis_route_output()["analysis_requirements"]
        invalid = _provider_final_route_narrative_output(
            requested,
            sections=[
                {
                    "step_ref": "invented_step",
                    "route_step": "核对未经供应的步骤。",
                    "expected_evidence": "获得未经供应的证据。",
                }
            ],
        )
        client, completions = _provider_client_with_outputs((invalid,))
        state = _provider_analysis_route_state(client)
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )

        finalized = workflow_module._finalize_production_analysis_route_narrative(
            state,
            route={"analysis_requirements": deepcopy(requirements)},
            requested=requested,
            registry=registry,
        )

        self.assertEqual(completions.attempt_count, 3)
        self.assertEqual(len(state["llm_calls"]), 1)
        self.assertEqual(finalized["route_narrative_status"], "unavailable")
        self.assertIn(
            "final_route_narrative_invalid:sections",
            finalized["route_narrative_failure"],
        )
        self.assertEqual(finalized["analysis_requirements"], requirements)

    def test_final_route_narrative_exhaustion_does_not_block_route_acceptance(self):
        plan = _provider_analysis_route_output()
        invalid_narrative = _provider_final_route_narrative_output(
            ("metric_timeseries",),
            sections=[
                {
                    "step_ref": "invented_step",
                    "route_step": "核对未经供应的步骤。",
                    "expected_evidence": "获得未经供应的证据。",
                }
            ],
        )
        client, _ = _provider_client_with_outputs((plan, invalid_narrative))
        state = _provider_analysis_route_state(client)

        workflow_module._design_analysis_route(state)
        accepted = workflow_module._accept_analysis_route(state)

        self.assertEqual(
            state["analysis_route"]["route_narrative_status"],
            "unavailable",
        )
        self.assertTrue(accepted["compiled_graph"].mutations.accepted_graph)

    def test_production_route_accept_rejects_stale_evidence_mapping(self):
        state = {
            "request": {"run_mode": "production", "question": "检查经营变化"},
            "intent": {
                "question_family": "pattern_explanation",
                "question_families": ["pattern_explanation"],
                "target_metric": "paid_amount",
                "pattern_family": "rolling",
                "requested_nodes": ("rolling_window_compare",),
            },
            "analysis_route": {
                **_provider_analysis_route_output(),
                "expected_evidence": {},
            },
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            "analysis_route_provider_contract_invalid:expected_evidence",
        ):
            workflow_module._accept_analysis_route(state)

    def test_trusted_fallback_rejection_rejects_extra_fields(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        with self.assertRaisesRegex(
            WorkflowFailure,
            "analysis_route_contract_invalid:obligation_mutation_history",
        ):
            workflow_module.reconcile_analysis_route(
                ("data_quality_profile",),
                {
                    "analysis_requirements": {
                        "target_metrics": ["paid_amount"],
                        "diagnostic_tags": [],
                    }
                },
                {
                    "question_family": "revenue_health_review",
                    "question_families": ["revenue_health_review"],
                    "target_metric": "paid_amount",
                },
                registry,
                trusted_prior_route={
                    "obligation_resolution": {
                        "status": "resolved",
                        "mutations": [
                            {
                                "action": "rejected",
                                "capability": "factor_topk",
                                "reason": (
                                    "diagnostic_question_family_incompatible"
                                ),
                                "extra": "forged",
                            }
                        ],
                    }
                },
            )

    def test_route_rejection_history_rejects_malformed_local_mutation(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        malformed = (
            {
                "action": "rejected",
                "capability": 42,
                "reason": "unknown_diagnostic_rejected",
            },
            {
                "action": "rejected",
                "capability": "   ",
                "reason": "unknown_diagnostic_rejected",
            },
            {
                "action": "rejected",
                "capability": "unknown_tag",
                "reason": "provider_supplied_rejection",
            },
            {
                "action": "rejected",
                "capability": "unknown_tag",
                "reason": "unknown_diagnostic_rejected",
                "extra": "forged",
            },
        )
        for mutation in malformed:
            with self.subTest(mutation=mutation), self.assertRaisesRegex(
                WorkflowFailure,
                "analysis_route_contract_invalid:obligation_mutation_history",
            ):
                workflow_module.reconcile_analysis_route(
                    ("data_quality_profile",),
                    {
                        "analysis_requirements": {
                            "target_metrics": ["paid_amount"],
                            "diagnostic_tags": [],
                        },
                    },
                    {
                        "question_family": "revenue_health_review",
                        "question_families": ["revenue_health_review"],
                        "target_metric": "paid_amount",
                    },
                    registry,
                    trusted_prior_route={
                        "obligation_resolution": {
                            "status": "resolved",
                            "mutation_history": [mutation],
                        }
                    },
                )

    def test_obligation_route_conflict_opens_existing_clarification_contract(self):
        state = {"route_material_conflicts": (), "boundary_decision": {}}
        route = {
            "obligation_resolution": {
                "status": "conflict",
                "error": "diagnostic_question_family_incompatible:factor_topk:data_quality_or_evidence_review",
            }
        }

        workflow_module._consume_obligation_route_conflict(state, route)

        self.assertIn("analysis_obligations", state["route_material_conflicts"])
        self.assertEqual(
            state["boundary_decision"]["boundary_status"], "needs_question"
        )
        self.assertIn(
            "route_material_conflicts",
            workflow_module.WorkflowState.__annotations__,
        )

    def test_question_family_values_accept_reviewed_shapes_and_reject_unknown_mapping(self):
        self.assertEqual(
            _question_family_values("anomaly_or_black_swan_review"),
            ["anomaly_or_black_swan_review"],
        )
        self.assertEqual(
            _question_family_values([
                "anomaly_or_black_swan_review",
                "custom_baseline_comparison",
            ]),
            ["anomaly_or_black_swan_review", "custom_baseline_comparison"],
        )
        self.assertEqual(
            _question_family_values({
                "primary_question_family": "anomaly_or_black_swan_review",
                "secondary_question_families": ["custom_baseline_comparison"],
            }),
            ["anomaly_or_black_swan_review", "custom_baseline_comparison"],
        )
        with self.assertRaisesRegex(
            WorkflowFailure,
            "question_families_mapping_contract_invalid",
        ):
            _question_family_values({"unexpected_family_key": "anomaly"})

    def test_delivery_reverify_authority_failure_does_not_call_llm_or_mutate_claims(self):
        failed = {
            "status": "failed",
            "admin_audit": {
                "verifier": {
                    "status": "failed",
                    "errors": [
                        {"code": "free_text_without_verified_claim"},
                        {"code": "reported_verifier_mismatch"},
                    ],
                }
            },
        }
        original_claims = [
            _claim_fixture(
                text="目标窗口相对基线发生变化。",
                evidence_refs=("compare_periods:ready",),
                numbers={"relative_change": 0.2},
                time_window="2026-06-02",
                claim_type="comparative_change",
            )
        ]
        state = {
            "request": {},
            "answer_text": "待交付答案",
            "draft_claims": deepcopy(original_claims),
            "evidence": [],
            "evidence_brief": {},
            "intent": {
                "target_metric": "active_users",
                "pattern_family": "custom_baseline",
                "scope": "full_sample",
                "time_window": "2026-06-02",
            },
            "llm_calls": [],
        }
        with patch(
            "bi_agent.runtime.langgraph_workflow.reverify_answer_package_for_delivery",
            return_value=failed,
        ) as reverify, patch(
            "bi_agent.runtime.langgraph_workflow._build_answer_package_from_state",
            return_value={"status": "draft", "internal_authority": "candidate"},
        ) as build_package, patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
        ) as repair_llm:
            result = _delivery_reverify_with_answer_repair(state)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(state["workflow_status"], "failed")
        self.assertEqual(
            state["workflow_failure_reason"],
            "delivery_reverify_failed:free_text_without_verified_claim,reported_verifier_mismatch",
        )
        self.assertEqual(state["draft_claims"], original_claims)
        repair_llm.assert_not_called()
        build_package.assert_called_once()
        reverify.assert_called_once()

    def test_empty_graph_repairs_material_requirements_and_blocks_boundary_only(self):
        empty = compile_graph(
            question_family="unsupported_family",
            target_metric="active_users",
            requested_nodes=(),
        )
        material = {
            "compiled_graph": empty,
            "analysis_route": {
                "analysis_requirements": {
                    "target_metrics": ["active_users"],
                    "claim_intents": ["comparative_change"],
                }
            },
            "repair_attempts": 0,
        }
        self.assertEqual(_route_after_accept_analysis(material), "repair")
        material["repair_attempts"] = 2
        self.assertEqual(_route_after_accept_analysis(material), "block")

        boundary_only = {
            "compiled_graph": empty,
            "analysis_route": {"analysis_requirements": {}},
            "repair_attempts": 0,
        }
        self.assertEqual(_route_after_accept_analysis(boundary_only), "block")

    def test_material_source_gaps_offer_progress_for_ready_sibling_or_boundary_terminal(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        gap_types = (
            "dataset_snapshot_unavailable_as_of",
            "source_unbound",
            "contract_partial",
        )
        for gap_type in gap_types:
            gap = {
                "gap_type": gap_type,
                "requires_clarification": True,
                "owner": "contract_owner",
                "affected_capabilities": ("event_evidence",),
                "repair_options": ("choose_supported_claim_intent",),
            }
            with self.subTest(gap_type=gap_type, path="ready_sibling"):
                projected = _business_query_gap_projection(
                    (gap,),
                    {"target_metric": "paid_amount", "time_window": "target_day"},
                    accepted_capabilities=("compare_periods", "event_evidence"),
                    registry=registry,
                )
                actions = projected[0]["allowed_actions"]
                self.assertEqual(actions[0]["action_kind"], "omit_unavailable_context")
                self.assertNotIn(
                    "continue_with_boundary_only",
                    {item["action_kind"] for item in actions},
                )
            with self.subTest(gap_type=gap_type, path="no_ready_capability"):
                projected = _business_query_gap_projection(
                    (gap,),
                    {"target_metric": "paid_amount", "time_window": "target_day"},
                    accepted_capabilities=("event_evidence",),
                    registry=registry,
                )
                actions = projected[0]["allowed_actions"]
                self.assertEqual(
                    actions[0]["action_kind"], "continue_with_boundary_only"
                )
                self.assertEqual(actions[1]["action_kind"], "wait_for_source")

    def test_accepted_boundary_terminal_does_not_reask_same_gap(self):
        from types import SimpleNamespace

        state = {
            "request": {
                "accepted_degradation_choice": {
                    "action_kind": "continue_with_boundary_only",
                    "affected_capabilities": ["event_evidence"],
                }
            },
            "analysis_runtime_result": SimpleNamespace(status="clarify"),
        }

        self.assertEqual(_route_after_query_repair(state), "degraded")
        self.assertTrue(state["accepted_degraded_query_outcome"])

    def test_query_gap_actions_group_atomic_affected_capabilities_and_stage_overflow(self):
        actions = (
            {
                "action_kind": "omit_unavailable_context",
                "business_semantics": "继续主指标分析并保留背景限制",
                "affected_capabilities": ["event_evidence"],
            },
            {
                "action_kind": "omit_unavailable_context",
                "business_semantics": "继续主指标分析并保留背景限制",
                "affected_capabilities": ["gameplay_activity_context"],
            },
            {
                "action_kind": "wait_for_source",
                "business_semantics": "等待相关业务数据可用",
                "affected_capabilities": ["event_evidence", "gameplay_activity_context"],
            },
            {
                "action_kind": "register_dataset_snapshot",
                "business_semantics": "登记缺失的数据快照",
                "affected_capabilities": ["restricted_context"],
            },
        )

        selected, staged = _group_query_gap_actions(actions)

        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[0]["action_kind"], "omit_unavailable_context")
        self.assertEqual(
            selected[0]["affected_capabilities"],
            ["event_evidence", "gameplay_activity_context"],
        )
        self.assertEqual(selected[1]["action_kind"], "wait_for_source")
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["action_kind"], "register_dataset_snapshot")
        self.assertTrue(selected[0]["choice_id"].startswith("query-gap-"))

        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        remaining, updated = _apply_query_gap_action_to_route(
            (
                "market_health_compare",
                "event_evidence",
                "gameplay_activity_context",
            ),
            {
                "analysis_requirements": {
                    "target_metrics": ["active_users"],
                    "context_sources": ["external_event", "gameplay"],
                    "claim_intents": ["comparative_change", "candidate_mechanism"],
                }
            },
            selected[0],
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )
        self.assertEqual(remaining, ("market_health_compare",))
        self.assertEqual(
            updated["analysis_requirements"]["context_sources"],
            ["external_event", "gameplay"],
        )
        self.assertEqual(
            updated["analysis_requirements"]["claim_intents"],
            ["comparative_change", "candidate_mechanism"],
        )

    def test_material_query_gap_without_feasible_action_routes_to_typed_block(self):
        from types import SimpleNamespace

        fake = ScriptedLLMClient({})
        state = {
            "request": {},
            "analysis_runtime_result": SimpleNamespace(
                typed_gaps=({
                    "gap_type": "unmapped_material_gap",
                    "requires_clarification": True,
                    "owner": "contract_owner",
                    "affected_capabilities": ("event_evidence",),
                    "repair_options": ("unreviewed_repair",),
                },),
            ),
            "query_repair_decisions": (),
            "compiled_graph": SimpleNamespace(
                mutations=SimpleNamespace(accepted_graph=("event_evidence",))
            ),
            "intent": {"target_metric": "active_users", "time_window": "target_day"},
            "llm_client": fake,
            "llm_calls": [],
            "checkpoint_events": [],
        }

        _generate_query_gap_clarification(state)

        self.assertTrue(state["query_gap_no_feasible_action"])
        self.assertEqual(state["workflow_status"], "blocked")
        self.assertEqual(_route_after_query_gap_clarification(state), "block")
        self.assertNotIn("query_gap_action_render", fake.calls)

    def test_query_gap_grouping_rejects_unknown_local_action_kind(self):
        with self.assertRaisesRegex(
            WorkflowFailure,
            "query_gap_action_contract_invalid:unknown_action",
        ):
            _group_query_gap_actions(({
                "action_kind": "unreviewed_action",
                "business_semantics": "执行未审核处理方式",
                "affected_capabilities": ["event_evidence"],
            },))

    def test_query_gap_action_binding_canonicalizes_provider_option_drift(self):
        business_gaps = [{
            "allowed_actions": [
                {
                    "choice_id": "continue",
                    "action_kind": "omit_unavailable_context",
                    "business_semantics": "继续主指标分析并说明限制",
                    "affected_capabilities": ["event_evidence"],
                },
                {
                    "choice_id": "wait",
                    "action_kind": "wait_for_source",
                    "business_semantics": "等待相关业务数据",
                    "affected_capabilities": ["event_evidence"],
                },
            ]
        }]

        escape = "tell the agent to do differently"
        provider_variants = (
            None,
            ["继续主指标分析并说明限制", escape],
            ["继续主指标分析并说明限制", "继续主指标分析并说明限制", escape],
            ["继续主指标分析并说明限制", "其他", escape],
            ["等待相关业务数据", "继续主指标分析并说明限制", escape],
            [" 继续主指标分析并说明限制", "等待相关业务数据", escape],
            [{"label": "继续主指标分析并说明限制"}, "等待相关业务数据", escape],
            ["继续主指标分析并说明限制", "等待相关业务数据", "Tell the agent to do differently"],
            ["继续主指标分析并说明限制", "等待相关业务数据", "按其他方式处理"],
            [escape, "继续主指标分析并说明限制", "等待相关业务数据"],
        )
        for options in provider_variants:
            with self.subTest(options=options):
                question = {"question": "如何继续？"}
                if options is not None:
                    question["options"] = options
                output = {
                    "questions": [question],
                    "recommended_assumption": {
                        "option": "继续主指标分析并说明限制"
                    },
                    "recommendation_reason": "符合当前业务证据边界。",
                }

                rendered, actions = _render_query_gap_actions(
                    {"llm_calls": []},
                    business_gaps,
                    output=output,
                )

                self.assertEqual(
                    rendered,
                    [
                        "继续主指标分析并说明限制（推荐）",
                        "等待相关业务数据",
                        escape,
                    ],
                )
                self.assertEqual(
                    [item["action_kind"] for item in actions],
                    ["omit_unavailable_context", "wait_for_source", "user_redirect"],
                )
                self.assertEqual(
                    [item["business_label"] for item in actions],
                    rendered,
                )
                self.assertIn(
                    "provider_query_gap_options_ignored",
                    output["advisory_risks"],
                )

    def test_query_gap_action_binding_defaults_drifted_recommendation(self):
        business_gaps = [{
            "allowed_actions": [{
                "choice_id": "continue",
                "action_kind": "omit_unavailable_context",
                "business_semantics": "继续主指标分析并说明限制",
                "affected_capabilities": ["event_evidence"],
            }]
        }]
        for recommendation in (
            " 继续主指标分析并说明限制 ",
            {"label": "继续主指标分析并说明限制"},
            "模型新增的处理方式",
            None,
        ):
            with self.subTest(recommendation=recommendation):
                output = {
                    "questions": [{"question": "如何继续？"}],
                    "recommended_assumption": {"option": recommendation},
                    "recommendation_reason": "符合当前业务证据边界。",
                }

                _, actions = _render_query_gap_actions(
                    {"llm_calls": []},
                    business_gaps,
                    output=output,
                )

                self.assertEqual(
                    output["recommended_assumption"],
                    {"option": "继续主指标分析并说明限制（推荐）"},
                )
                self.assertEqual(
                    output["recommended_choice_id"],
                    actions[0]["choice_id"],
                )
                self.assertTrue(actions[0]["business_reason"])
                self.assertIn(
                    "provider_query_gap_recommendation_overridden",
                    output["advisory_risks"],
                )

    def test_query_gap_action_binding_forced_ready_option_has_priority(self):
        business_gaps = [{
            "allowed_actions": [
                {
                    "choice_id": "wait",
                    "action_kind": "wait_for_source",
                    "business_semantics": "等待相关业务数据",
                    "affected_capabilities": ["event_evidence"],
                },
                {
                    "choice_id": "continue",
                    "action_kind": "omit_unavailable_context",
                    "business_semantics": "继续主指标分析并说明限制",
                    "affected_capabilities": ["event_evidence"],
                },
            ]
        }]
        output = {
            "questions": [{"question": "如何继续？"}],
            "recommended_assumption": {"option": "等待相关业务数据"},
            "recommendation_reason": "符合当前业务证据边界。",
        }

        _, actions = _render_query_gap_actions(
            {"llm_calls": []},
            business_gaps,
            output=output,
            forced_recommended_option="继续主指标分析并说明限制",
        )

        self.assertEqual(
            output["recommended_assumption"],
            {"option": "继续主指标分析并说明限制（推荐）"},
        )
        self.assertEqual(actions[0]["action_kind"], "omit_unavailable_context")
        self.assertTrue(actions[0]["business_reason"])
        self.assertIn(
            "provider_query_gap_recommendation_overridden",
            output["advisory_risks"],
        )

    def test_query_gap_invalid_question_or_reason_retries_three_provider_attempts(self):
        from types import SimpleNamespace

        valid = {
            "questions": [{"question": "如何继续？"}],
            "recommended_assumption": {"option": "等待相关业务数据可用后再恢复本次分析"},
            "recommendation_reason": "该选择符合当前业务证据边界。",
            "decision_summary": "需要用户确认处理方式。",
            "display_summary": "等待用户确认业务口径。",
        }
        for invalid in (
            {**valid, "questions": []},
            {**valid, "recommendation_reason": ""},
        ):
            with self.subTest(invalid=invalid):
                client, completions = _provider_client_with_outputs((invalid,))
                state = {
                    "request": {},
                    "analysis_route": {"requested_nodes": ["event_evidence"]},
                    "analysis_runtime_result": SimpleNamespace(
                        typed_gaps=({
                            "gap_type": "source_unbound",
                            "requires_clarification": True,
                            "owner": "data_owner",
                            "affected_capabilities": ("event_evidence",),
                            "repair_options": ("bind_source",),
                        },),
                        bound_capability_inputs={},
                    ),
                    "query_repair_decisions": (),
                    "intent": {
                        "target_metric": "active_users",
                        "time_window": "target_day",
                    },
                    "llm_client": client,
                    "llm_calls": [],
                    "checkpoint_events": [],
                }

                with self.assertRaises(WorkflowFailure):
                    _generate_query_gap_clarification(state)

                self.assertEqual(completions.attempt_count, 3)
                self.assertEqual(state["llm_calls"][-1]["attempt_count"], 3)

    def test_query_gap_action_binding_rejects_missing_recommendation_reason(self):
        business_gaps = [{
            "allowed_actions": [{
                "choice_id": "continue",
                "action_kind": "omit_unavailable_context",
                "business_semantics": "继续主指标分析并说明限制",
                "affected_capabilities": ["event_evidence"],
            }]
        }]
        output = {
            "questions": [{
                "question": "如何继续？",
                "options": [
                    "继续主指标分析并说明限制",
                    "tell the agent to do differently",
                ],
            }],
            "recommended_assumption": {
                "option": "继续主指标分析并说明限制"
            },
            "recommendation_reason": "",
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            "query_gap_action_binding_invalid:recommendation_reason",
        ):
            _render_query_gap_actions(
                {"llm_calls": []},
                business_gaps,
                output=output,
            )

    def test_window_coverage_terminal_does_not_open_query_gap_choices(self):
        gap = _business_query_repair_gap(({
            "action": "block",
            "reason": "window_coverage_failure",
            "requires_clarification": False,
            "failed_query_contract_ref": "query:internal",
        },))

        self.assertEqual(gap, {})

    def test_answer_package_canonicalizes_accepted_degradation_from_manifest(self):
        from bi_agent.runtime.langgraph_workflow import _build_answer_package_from_state

        choice = {
            "action_kind": "omit_unavailable_context",
            "affected_capabilities": ["event_evidence"],
            "source_run_id": "run-source",
        }
        package = _build_answer_package_from_state({
            "run_id": "run-with-accepted-assumption",
            "request": {
                "context_manifest": {"accepted_assumptions": [choice]},
                "compiler_runtime_plan": {"graph_metadata": {}},
            },
            "checkpoint_events": [],
            "validator_results": [],
        })

        self.assertEqual(package["accepted_degradation_choice"], choice)
        self.assertEqual(package["context_assumptions"], [choice])
        self.assertEqual(
            package["accepted_graph_metadata"]["accepted_assumptions"],
            [choice],
        )

    def test_attempt_authority_choice_keeps_zero_claim_terminal_delivery_verifiable(self):
        choice = {
            "choice_id": "continue-with-reviewed-boundary",
            "action_kind": "continue_with_boundary_only",
            "affected_capabilities": ["unavailable_context"],
            "source_run_id": "run-prior-clarification",
        }
        authority_sources = {
            "state": lambda state: state.update(
                {"accepted_assumptions": [choice]}
            ),
            "attempt": lambda state: state["request"].update({
                "clarification_attempt_context": {
                    "accepted_degradation_choice": choice,
                }
            }),
            "manifest": lambda state: state["request"].update({
                "context_manifest": {"accepted_assumptions": [choice]},
            }),
        }

        for authority_source, attach_choice in authority_sources.items():
            with self.subTest(authority_source=authority_source):
                state = {
                    "run_id": f"run-terminal-{authority_source}",
                    "request": {},
                    "checkpoint_events": [],
                    "validator_results": [
                        {"ok": False, "code": "required_context_unavailable"}
                    ],
                    "draft_claims": [],
                    "evidence": [],
                }
                attach_choice(state)
                state["final_explanation"] = _sanitize_terminal_explanation(
                    {
                        "explanation": "当前边界下没有可发布的业务结论。",
                        "repair_path": "补齐已确认缺口后重新分析。",
                    },
                    state,
                    "degraded",
                )

                package = _build_answer_package_from_state(state)

                self.assertEqual(
                    package["admin_audit"]["verifier"]["status"],
                    "passed",
                    package["admin_audit"]["verifier"]["errors"],
                )
                self.assertEqual(
                    set(package["final_explanation"]["used_next_action_ids"]),
                    {choice["choice_id"], choice["action_kind"]},
                )
                with patch(
                    "bi_agent.runtime.langgraph_workflow._invoke_llm"
                ) as repair_llm:
                    delivered = _delivery_reverify_with_answer_repair(state)

                repair_llm.assert_not_called()
                self.assertEqual(delivered["status"], "draft")
                self.assertEqual(
                    delivered["admin_audit"]["verifier"]["status"],
                    "passed",
                )
                self.assertEqual(
                    delivered["final_explanation"],
                    state["final_explanation"],
                )
                self.assertNotIn("workflow_failure_reason", state)

    def test_attempt_authority_choice_uses_closed_source_precedence(self):
        def choice(source):
            return {
                "choice_id": f"choice-{source}",
                "action_kind": f"action_{source}",
                "affected_capabilities": [f"capability_{source}"],
            }

        state_choice = choice("state")
        state = {
            "run_id": "run-terminal-authority-precedence",
            "accepted_assumptions": [state_choice],
            "request": {
                "accepted_degradation_choice": choice("request"),
                "clarification_attempt_context": {
                    "accepted_degradation_choice": choice("attempt"),
                },
                "context_manifest": {
                    "accepted_assumptions": [choice("manifest")],
                },
            },
            "checkpoint_events": [],
            "validator_results": [
                {"ok": False, "code": "required_context_unavailable"}
            ],
            "draft_claims": [],
            "evidence": [],
        }

        state["final_explanation"] = _sanitize_terminal_explanation(
            {
                "explanation": "当前边界下没有可发布的业务结论。",
                "repair_path": "补齐已确认缺口后重新分析。",
            },
            state,
            "degraded",
        )
        package = _build_answer_package_from_state(state)

        self.assertEqual(package["accepted_degradation_choice"], state_choice)
        self.assertEqual(
            set(state["final_explanation"]["used_next_action_ids"]),
            {state_choice["choice_id"], state_choice["action_kind"]},
        )
        self.assertEqual(
            package["admin_audit"]["verifier"]["status"],
            "passed",
            package["admin_audit"]["verifier"]["errors"],
        )

    def test_workflow_entry_promotes_manifest_only_assumption_into_runtime_request(self):
        choice = {
            "action_kind": "omit_unavailable_context",
            "affected_capabilities": ["event_evidence"],
            "source_run_id": "run-source",
        }
        captured = {}

        class CapturingGraph:
            def invoke(self, state, config):
                captured["request"] = deepcopy(state["request"])
                captured["accepted_assumptions"] = deepcopy(
                    state["accepted_assumptions"]
                )
                projected_request = deepcopy(state["request"])
                projected_request.pop("accepted_degradation_choice", None)
                projected_request.pop("context_manifest", None)
                projected_state = {**state, "request": projected_request}
                return {
                    **projected_state,
                    "workflow_status": "draft",
                    "answer_package": _build_answer_package_from_state(projected_state),
                }

        with patch(
            "bi_agent.runtime.langgraph_workflow.build_pattern_graph",
            return_value=CapturingGraph(),
        ):
            result = workflow_module.run_pattern_workflow({
                "run_id": "run-with-manifest-assumption",
                "llm_client": object(),
                "context_manifest": {"accepted_assumptions": [choice]},
            })

        self.assertEqual(captured["request"]["accepted_degradation_choice"], choice)
        self.assertEqual(captured["accepted_assumptions"], [choice])
        self.assertEqual(result.answer_package["context_assumptions"], [choice])
        self.assertEqual(result.answer_package["accepted_degradation_choice"], choice)
        self.assertEqual(
            result.answer_package["accepted_graph_metadata"]["accepted_assumptions"],
            [choice],
        )

    def test_workflow_and_persistence_share_answer_package_build_context(self):
        request = {
            "run_id": "run-shared-build-context",
            "thread_id": "thread-shared-build-context",
            "topic_id": "topic-shared-build-context",
            "context_manifest": {"manifest_id": "context-input", "items": []},
            "reuse_decisions": [],
            "artifact_root": "artifacts/task10-core",
        }
        artifact_path = (
            "artifacts/task10-core/run-shared-build-context/answer_package.json"
        )
        expected = AnswerPackageBuildContext.create(
            request=request,
            artifact_path=artifact_path,
        )
        state = {
            "run_id": request["run_id"],
            "request": request,
            "checkpoint_events": [],
            "draft_claims": [],
            "evidence": [],
            "validator_results": [],
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow.build_answer_package",
            side_effect=lambda **kwargs: kwargs,
        ):
            workflow_kwargs = _build_answer_package_from_state(state)

        self.assertEqual(
            workflow_kwargs["context_manifest"],
            dict(expected.context_owner),
        )
        self.assertEqual(
            workflow_kwargs["trusted_claim_provenance_record"],
            dict(expected.trusted_provenance),
        )
        tampered = AnswerPackageBuildContext.create(
            request=request,
            artifact_path="artifacts/tampered/answer_package.json",
        )
        self.assertEqual(
            tampered.trusted_provenance["record_ref"],
            expected.trusted_provenance["record_ref"],
        )

    def test_analysis_runtime_executes_exact_slot_and_persists_complete_zero_claim_chain(self):
        from bi_agent.conversation.store import InMemoryConversationStore
        from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult
        from bi_agent.runtime.dataset_catalog import DatasetCatalog
        from bi_agent.runtime.evidence_authority import (
            EvidenceIntegrityError,
            RuntimeEvidenceAuthority,
        )
        from bi_agent.runtime.query_executor import ClickHouseQueryExecutor
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
        from tests.phase4.test_analysis_contract_compiler import (
            canonical_release_catalog,
            snapshot,
        )

        class CompleteRuntime:
            def aggregate(self, sql, query_id, **kwargs):
                return ClickHouseQueryResult(
                    ok=True,
                    query_id=query_id,
                    rows=(
                        {
                            "window_id": "target_day",
                            "window_role": "target",
                            "observation_key": "2026-06-02",
                            "paid_amount": 120.0,
                        },
                        {
                            "window_id": "previous_day",
                            "window_role": "baseline",
                            "observation_key": "2026-06-01",
                            "paid_amount": 100.0,
                        },
                    ),
                )

            bounded_context = aggregate

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        authority = RuntimeEvidenceAuthority(runtime_registry=registry)
        catalog, release_resolver, _ = canonical_release_catalog(
            snapshot("paid_order_success", "paid", "2026-07-04")
        )
        runtime = AnalysisRuntime(
            catalog=catalog,
            registry=registry,
            executor=ClickHouseQueryExecutor(
                CompleteRuntime(),
                evidence_resolver=authority,
                rows_loader=authority.rows_loader,
                evidence_writer=authority._runtime_writer(),
            ),
            release_resolver=release_resolver,
            evidence_authority=authority,
        )
        request = AnalysisRuntimeRequest.create(
            run_id="run-runtime-complete",
            proposal={
                "question_families": ["custom_baseline_comparison"],
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
                "scope": {"type": "full_sample"},
                "target_semantic": "yesterday",
                "baselines": ["previous_day"],
            },
            accepted_graph=("compare_periods",),
            as_of="2026-06-03T12:00:00+01:00",
        )

        result = runtime.execute(request)
        typed_graph = _typed_clarification_compiled_graph(
            runtime.compile(request),
            {
                "requested_nodes": ["compare_periods"],
                "target_claim": "comparative_change",
            },
        )
        missing_runtime = AnalysisRuntime(
            catalog=DatasetCatalog(()),
            registry=registry,
            executor=runtime.executor,
            release_resolver=None,
            evidence_authority=authority,
        )
        hard_gap_graph = _typed_clarification_compiled_graph(
            missing_runtime.compile(request),
            {
                "requested_nodes": ["compare_periods"],
                "target_claim": "comparative_change",
            },
        )
        bundle = runtime.build_persistence_bundle(
            result,
            answer_package={"status": "draft", "sections": []},
            request={
                "run_id": "run-runtime-complete",
                "thread_id": "thread-runtime-complete",
                "topic_id": "topic-runtime-complete",
                "context_manifest": {"manifest_id": "context-runtime", "items": []},
            },
            artifact_path="artifacts/task10-core/run-runtime-complete.json",
        )
        store = InMemoryConversationStore()

        self.assertEqual(result.status, "ready")
        self.assertEqual(typed_graph.status, "accepted")
        self.assertEqual(
            typed_graph.mutations.accepted_graph,
            ("compare_periods",),
        )
        self.assertIsNone(hard_gap_graph)
        self.assertEqual(result.completeness_reports[0].completeness_status, "complete")
        self.assertEqual(result.bound_capability_inputs["compare_periods"].status, "ready")
        self.assertEqual(
            tuple(result.bound_capability_inputs["compare_periods"].rows_by_slot),
            ("daily_metric_baselines",),
        )
        self.assertEqual(
            store.save_analysis_runtime_records(
                run_id="run-runtime-complete",
                **bundle,
            ),
            "published",
        )
        self.assertEqual(bundle["verified_claims"], ())

        binding = result.persistence_records["capability_binding_records"][0]
        evidence_ref = "evidence:runtime-complete"
        verified_claim = {
            "text": "目标日付费金额高于前一日。",
            "claim_type": "comparative_change",
            "claim_strength": "observed",
            "evidence_refs": [evidence_ref],
            "numbers": {},
        }
        claim_answer_package = {
            "run_id": "run-runtime-complete",
            "status": "draft",
            "admin_audit": {"verified_claims": [verified_claim]},
            "sections": [
                {
                    "section_id": "summary",
                    "payload": {"claims": [verified_claim]},
                },
                {
                    "section_id": "evidence",
                    "payload": {
                        "evidence": [
                            {
                                "evidence_ref": evidence_ref,
                                "binding_manifest_ref": binding.record_ref,
                            }
                        ]
                    },
                },
            ],
        }
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "analysis_runtime_verified_claim_authority_missing",
        ):
            runtime.build_persistence_bundle(
                result,
                answer_package={
                    **claim_answer_package,
                    "admin_audit": {},
                },
                request={
                    "run_id": "run-runtime-complete",
                    "thread_id": "thread-runtime-complete",
                    "topic_id": "topic-runtime-complete",
                    "context_manifest": {
                        "manifest_id": "context-runtime",
                        "items": [],
                    },
                },
                artifact_path=(
                    "artifacts/task10-core/run-runtime-complete.json"
                ),
            )
        claim_bundle = runtime.build_persistence_bundle(
            result,
            answer_package=claim_answer_package,
            request={
                "run_id": "run-runtime-complete",
                "thread_id": "thread-runtime-complete",
                "topic_id": "topic-runtime-complete",
                "context_manifest": {"manifest_id": "context-runtime", "items": []},
            },
            artifact_path="artifacts/task10-core/run-runtime-complete.json",
        )
        from tests.phase7.artifact_test_support import (
            bind_answer_package_artifact,
        )

        bind_answer_package_artifact(
            claim_bundle,
            run_id="run-runtime-complete",
            answer_package=claim_answer_package,
        )

        self.assertEqual(len(claim_bundle["verified_claims"]), 1)
        self.assertEqual(
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-runtime-complete",
                **claim_bundle,
            ),
            "published",
        )

        second_request = AnalysisRuntimeRequest.create(
            run_id="run-runtime-complete-second",
            proposal=dict(request.proposal),
            accepted_graph=request.accepted_graph,
            as_of=request.as_of,
        )
        second_result = runtime.execute(second_request)
        original_bound = result.bound_capability_inputs["compare_periods"]
        second_bound = second_result.bound_capability_inputs["compare_periods"]

        self.assertEqual(second_result.status, "ready")
        self.assertNotEqual(
            second_result.analysis_contract.analysis_contract_id,
            result.analysis_contract.analysis_contract_id,
        )
        self.assertNotEqual(
            second_bound.analysis_contract_ref,
            original_bound.analysis_contract_ref,
        )
        self.assertNotEqual(
            second_bound.binding_manifest_ref,
            original_bound.binding_manifest_ref,
        )
        self.assertNotEqual(
            second_bound.binding_manifest_digest,
            original_bound.binding_manifest_digest,
        )
        self.assertNotEqual(second_bound.result_refs, original_bound.result_refs)
        self.assertTrue(
            all(
                "run-runtime-complete-second" in ref
                for ref in second_bound.query_contract_refs
            )
        )

    def test_preexecution_query_contracts_include_snapshot_authority_records(self):
        from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult
        from bi_agent.runtime.dataset_catalog import DatasetCatalog
        from bi_agent.runtime.evidence_authority import RuntimeEvidenceAuthority
        from bi_agent.runtime.query_executor import ClickHouseQueryExecutor
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
        from tests.phase4.test_analysis_contract_compiler import (
            canonical_release_catalog,
            snapshot,
        )

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        authority = RuntimeEvidenceAuthority(runtime_registry=registry)
        catalog, release_resolver, _ = canonical_release_catalog(
            snapshot("paid_order_success", "paid", "2026-07-04")
        )

        class CompletePatternRuntime:
            def aggregate(self, sql, query_id, **kwargs):
                return ClickHouseQueryResult(
                    ok=True,
                    query_id=query_id,
                    rows=(
                        {
                            "window_id": "target_day",
                            "window_role": "target",
                            "observation_key": "2026-06-02",
                            "paid_amount": 120.0,
                        },
                        {
                            "window_id": "previous_day",
                            "window_role": "baseline",
                            "observation_key": "2026-06-01",
                            "paid_amount": 100.0,
                        },
                    ),
                )

            bounded_context = aggregate

        runtime = AnalysisRuntime(
            catalog=catalog,
            registry=registry,
            executor=ClickHouseQueryExecutor(
                CompletePatternRuntime(),
                evidence_resolver=authority,
                rows_loader=authority.rows_loader,
                evidence_writer=authority._runtime_writer(),
                release_resolver=release_resolver,
            ),
            release_resolver=release_resolver,
            evidence_authority=authority,
        )
        request = AnalysisRuntimeRequest.create(
            run_id="run-preexecution-snapshot-authority",
            proposal={
                "question_families": ["recurring_pattern_analysis"],
                "target_metrics": ["paid_amount"],
                "claim_intents": ["recurring_pattern_existence"],
                "target_semantic": "yesterday",
                "baselines": ["previous_day"],
            },
            accepted_graph=("pattern_scan", "segment_contribution"),
            as_of="2026-06-03T12:00:00+01:00",
        )

        result = runtime.execute(request)
        expected_snapshot_refs = {
            ref
            for contract in result.query_contracts
            for ref in contract.dataset_snapshot_refs
        }

        self.assertTrue(result.query_contracts)
        self.assertTrue(result.query_results)
        self.assertEqual(
            {
                record.snapshot_ref
                for record in result.persistence_records["snapshot_records"]
            },
            expected_snapshot_refs,
        )

    def test_analysis_runtime_passes_release_authority_to_query_validation(self):
        from bi_agent.conversation.store import InMemoryConversationStore
        from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult
        from bi_agent.runtime.evidence_authority import RuntimeEvidenceAuthority
        from bi_agent.runtime.query_executor import ClickHouseQueryExecutor
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
        from tests.phase4.test_analysis_contract_compiler import (
            _market_dashboard_snapshots,
            released_catalog,
        )

        class CompleteMarketRuntime:
            def aggregate(self, sql, query_id, **kwargs):
                return ClickHouseQueryResult(
                    ok=True,
                    query_id=query_id,
                    rows=(
                        {
                            "window_id": "target_day",
                            "window_role": "target",
                            "observation_key": "2026-06-02",
                            "active_users": 120.0,
                        },
                        {
                            "window_id": "previous_day",
                            "window_role": "baseline",
                            "observation_key": "2026-06-01",
                            "active_users": 100.0,
                        },
                    ),
                )

            bounded_context = aggregate

        catalog = released_catalog(*_market_dashboard_snapshots())
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        authority = RuntimeEvidenceAuthority(runtime_registry=registry)
        runtime = AnalysisRuntime(
            catalog=catalog,
            registry=registry,
            executor=ClickHouseQueryExecutor(
                CompleteMarketRuntime(),
                evidence_resolver=authority,
                rows_loader=authority.rows_loader,
                evidence_writer=authority._runtime_writer(),
                release_resolver=catalog._release_resolver,
            ),
            release_resolver=catalog._release_resolver,
            evidence_authority=authority,
        )
        request = AnalysisRuntimeRequest.create(
            run_id="run-runtime-release",
            proposal={
                "question_families": ["market_health_comparison"],
                "target_metrics": ["active_users"],
                "claim_intents": ["comparative_change"],
                "scope": {"type": "full_sample"},
                "target_semantic": "yesterday",
                "baselines": ["previous_day"],
            },
            accepted_graph=("market_health_compare",),
            as_of="2026-06-03T12:00:00+01:00",
        )

        result = runtime.execute(request)
        bundle = runtime.build_persistence_bundle(
            result,
            answer_package={"status": "failed", "sections": []},
            request={
                "run_id": request.run_id,
                "thread_id": "thread-runtime-release",
                "topic_id": "topic-runtime-release",
            },
            artifact_path="artifacts/task10-core/run-runtime-release.json",
        )

        self.assertEqual(len(result.query_results), 1)
        self.assertEqual(
            result.query_contracts[0].dataset_snapshot_refs,
            ("snapshot:market:capability-local",),
        )
        self.assertEqual(
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id=request.run_id,
                **bundle,
            ),
            "published",
        )

    def test_analysis_contract_gap_that_changes_the_conclusion_requests_clarification(self):
        runtime_result = AnalysisRuntimeResult(
            analysis_contract=object(),
            query_contracts=(),
            query_results=(),
            completeness_reports=(),
            capability_plans=(),
            bound_capability_inputs={},
            repair_decisions=(),
            typed_gaps=({"requires_clarification": True},),
            persistence_records={},
        )

        self.assertEqual(runtime_result.status, "clarify")

    def test_partially_bound_required_slots_are_not_executable_ready(self):
        from types import SimpleNamespace

        gap = ContractGap(
            gap_type="missing_contract",
            gap_id="gap:segment-contribution-partial",
            requires_clarification=True,
            affected_capabilities=("segment_contribution",),
        )
        outcome = SimpleNamespace(
            analysis_contract=SimpleNamespace(contract_gaps=(gap,)),
            query_contracts=(
                SimpleNamespace(query_contract_id="query:segment:bound"),
            ),
            capability_plans=(
                _capability_plan_fixture(
                    "segment_contribution",
                    required_query_refs=(
                        ("query:segment:bound",),
                        (),
                    ),
                ),
            ),
        )

        self.assertFalse(analysis_outcome_has_executable_ready_capability(outcome))
        self.assertTrue(analysis_outcome_requires_preexecution_clarification(outcome))

        class ExecutorMustNotRun:
            def execute(self, *args, **kwargs):
                raise AssertionError("partial_required_plan_must_stop_preexecution")

        runtime = object.__new__(AnalysisRuntime)
        runtime.executor = ExecutorMustNotRun()
        runtime._catalog_provider = lambda: SimpleNamespace(snapshots=lambda: ())
        runtime._compile_with_catalog = lambda request, catalog: outcome
        runtime._authority_records = lambda compiled, results, bound, **kwargs: {}
        result = runtime.execute(
            AnalysisRuntimeRequest.create(
                run_id="run-partial-required-plan",
                proposal={"target_metrics": ["paid_amount"]},
                accepted_graph=("segment_contribution",),
                as_of="2026-06-03T12:00:00+01:00",
            )
        )
        self.assertEqual(result.query_results, ())

    def test_executed_rows_do_not_degrade_a_blocked_completeness_boundary(self):
        from types import SimpleNamespace

        runtime_result = AnalysisRuntimeResult(
            analysis_contract=object(),
            query_contracts=(),
            query_results=(object(),),
            completeness_reports=(SimpleNamespace(analysis_readiness="blocked"),),
            capability_plans=(),
            bound_capability_inputs={},
            repair_decisions=(SimpleNamespace(action="degrade"),),
            typed_gaps=(),
            persistence_records={},
        )

        self.assertEqual(runtime_result.status, "blocked")

    def test_answer_package_carries_typed_runtime_payload_after_query_repair(self):
        package = _build_answer_package_from_state(
            {
                "run_id": "run-typed-package",
                "request": {
                    "analysis_contract": {"analysis_contract_id": "analysis:typed"},
                    "query_contracts": [{"query_contract_id": "query:typed"}],
                    "query_results": [{"result_ref": "result:typed"}],
                    "completeness_reports": [{"report_ref": "complete:typed"}],
                    "capability_execution_plans": [
                        asdict(
                            _capability_plan_fixture(
                                "compare_periods",
                                required_query_refs=(("query:typed",),),
                            )
                        )
                    ],
                    "repair_decisions": [
                        {"failed_signature": "signature", "action": "degrade"}
                    ],
                },
                "checkpoint_events": [],
                "draft_claims": [],
                "evidence": [],
                "validator_results": [],
                "final_explanation": {
                    "status": "blocked",
                    "explanation": "结果完整性不足。",
                    "repair_path": "刷新后重试。",
                },
            }
        )

        self.assertEqual(
            package["admin_audit"]["analysis_contract"]["analysis_contract_id"],
            "analysis:typed",
        )
        self.assertEqual(len(package["admin_audit"]["query_contracts"]), 1)
        self.assertEqual(len(package["admin_audit"]["repair_attempts"]), 1)

    def test_prompt_specs_are_consistent(self):
        self.assertEqual(validate_prompt_specs(), [])

    def test_conversation_orchestrator_prompt_keeps_llm_in_business_routing_role(self):
        messages = build_prompt("conversation_orchestrator", {"user_message": "check"}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("Classify one user message inside a BI investigation thread", text)
        self.assertIn("Allowed intent values", text)
        self.assertIn("Allowed topic_relation values", text)
        self.assertIn("Use ask_topic_choice", text)
        self.assertIn("Do not answer the BI question", text)
        self.assertIn("Simplified Chinese business wording", text)

    def test_business_intent_prompt_separates_repeated_patterns_from_baseline_comparison(self):
        messages = build_prompt("business_intent", {"question": "check"}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("one-off baseline/target comparison", text)
        self.assertIn("repeated time shape", text)
        self.assertIn("weekday-vs-weekday inside many weeks", text)
        self.assertIn("month start/boundary/mid/end inside many months", text)
        self.assertIn("rolling-window trends", text)
        self.assertIn("use business labels", text)
        self.assertIn("付费金额", text)
        self.assertIn("paid_amount", text)

    def test_business_intent_prompt_types_optional_answer_contract(self):
        messages = build_prompt("business_intent", {"question": "check"}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("answer_contract must be a JSON object", text)
        self.assertIn("omit answer_contract or use {}", text)

    def test_business_intent_prompt_requires_exact_ordered_baseline_ids(self):
        messages = build_prompt(
            "business_intent",
            {
                "question": "将多个业务基线按优先级比较。",
                "allowed_baseline_ids": [
                    "previous_day",
                    "rolling_7_day_baseline",
                    "same_weekday_last_week",
                ],
                "allowed_baseline_semantics": [
                    {
                        "id": "previous_day",
                        "label": "前一天",
                        "semantics": "目标日之前的一个完整自然日",
                    }
                ],
            },
        ).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("allowed_baseline_ids", text)
        self.assertIn("allowed_baseline_semantics", text)
        self.assertIn("exact string ids", text)
        self.assertIn("user's requested priority order", text)
        self.assertIn("target window", text)
        self.assertIn("use []", text)
        self.assertIn("Never return objects", text)


    def test_business_intent_prompt_keeps_bound_material_out_of_ambiguous_slots(self):
        messages = build_prompt(
            "business_intent",
            {
                "question": "继续检查当前经营表现。",
                "bound_business_context": {
                    "target_metric": "paid_amount",
                    "scope": "full_sample",
                    "time_window": "yesterday",
                },
            },
        ).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("ambiguous_slots", text)
        self.assertIn("still unbound", text)
        self.assertIn("would change the business answer", text)
        self.assertIn("must not also appear in ambiguous_slots", text)
        self.assertIn("non-null, non-empty value", text)
        self.assertIn("copy its exact canonical value", text)
        self.assertIn("explicitly replaces that axis", text)

    def test_business_intent_prompt_separates_canonical_target_day_from_baseline(self):
        payload = workflow_module._business_intent_payload(
            {
                "question": (
                    "昨天付费金额为什么变化？主要是首充人数、付费频次、"
                    "单笔付费金额，还是支付成功率等因素导致的？"
                ),
                "run_mode": "live",
            }
        )
        spec = build_prompt("business_intent", payload)
        text = "\n".join(message["content"] for message in spec.messages)

        self.assertEqual(payload["allowed_relative_target_ids"], ["yesterday"])
        self.assertIn("canonical target-window machine field", text)
        self.assertIn("return exactly yesterday", text)
        self.assertIn("previous_day belongs only in baseline_candidates", text)
        self.assertIn("return []", text)

    def test_business_intent_prompt_scopes_null_rule_away_from_required_material(self):
        messages = build_prompt(
            "business_intent",
            {
                "question": "请按推荐时间口径继续。",
                "reviewed_time_window_recommendation": {
                    "time_window": "2026-06-02",
                    "source": "analysis_context.target_date",
                },
            },
        ).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("reviewed_time_window_recommendation", text)
        self.assertIn("delegates the time-window choice", text)
        self.assertIn("copy its time_window value exactly", text)
        self.assertIn("Required material fields must never use null", text)
        self.assertIn("evidence-derived facts", text)
        self.assertNotIn("If evidence is missing, use null", text)

    def test_business_intent_prompt_requires_canonical_pattern_contract(self):
        spec = build_prompt(
            "business_intent",
            {"question": "检查一个有界业务窗口的经营表现。"},
        )
        text = "\n".join(message["content"] for message in spec.messages)

        self.assertIn("pattern_params", spec.required_keys)
        for family in (
            "intra_period",
            "weekly",
            "event_relative",
            "rolling",
            "lag_recovery",
            "custom_baseline",
        ):
            with self.subTest(family=family):
                self.assertIn(family, text)
        self.assertIn("Never return null, none, or an invented pattern family", text)
        self.assertIn("single bounded observation window", text)
        self.assertIn("target_weekday", text)
        self.assertIn("target_weekdays", text)
        self.assertIn("non-empty flat sequence", text)
        self.assertIn("target_phase", text)
        self.assertIn("target_group", text)
        self.assertIn("pattern_params must be a JSON object", text)

    def test_coverage_uses_baseline_rows_when_quality_probe_is_primary(self):
        state = {
            "validator_results": [{"ok": True}],
            "request": {
                "required_fields": ("period", "group", "amount", "orders"),
                "rows": (
                    {
                        "period": "2026-07-08",
                        "group": "target",
                        "orders": 10,
                        "paid_users": 8,
                        "min_period": "2026-07-01",
                        "max_period": "2026-07-08",
                    },
                ),
                "runtime_rows_by_intent": {
                    "daily_metric_baselines": (
                        {
                            "period": "2026-07-07",
                            "group": "previous_day",
                            "amount": 90.0,
                            "orders": 9,
                        },
                        {
                            "period": "2026-07-08",
                            "group": "target",
                            "amount": 120.0,
                            "orders": 10,
                        },
                    ),
                    "data_quality_probe": (
                        {
                            "period": "2026-07-08",
                            "group": "target",
                            "orders": 10,
                            "paid_users": 8,
                        },
                    ),
                },
            },
            "intent": {
                "pattern_family": "daily_change",
                "pattern_params": {},
            },
        }

        self.assertIn("本地聚合结果", _local_coverage_answerable_reason(state))

    def test_workflow_compiler_uses_row_provider_schema_fields(self):
        class Provider:
            def schema_fields(self):
                return (
                    "business_date_lagos",
                    "paid_amount_ngn",
                    "user_id",
                    "channel",
                    "payment_method",
                    "package_name",
                    "gameplay_id",
                    "payment_status",
                    "order_id",
                )

        context = _compiler_bound_context(
            {
                "request": {"row_provider": Provider()},
                "intent": {
                    "pattern_family": "custom_baseline",
                    "target_metric": "paid_amount",
                },
                "analysis_route": {},
            }
        )

        self.assertIn("package_name", context["clickhouse_schema_fields"])
        self.assertIn("gameplay_id", context["clickhouse_schema_fields"])
        self.assertIsInstance(context["clickhouse_schema_fields"], tuple)

    def test_boundary_decision_prompt_hides_internal_tokens_from_business_text(self):
        messages = build_prompt("boundary_decision", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("shown to business users", text)
        self.assertIn("Do not expose internal field names", text)
        self.assertIn("full_sample", text)
        self.assertIn("mid_phase", text)
        self.assertIn("全样本", text)
        self.assertIn("月中窗口", text)
        self.assertIn("稳定性要求", text)
        self.assertIn("Do not ask only to", text)
        self.assertIn("stable higher", text)
        self.assertIn("month start compared with mid/end", text)
        self.assertIn("exclusive end dates", text)
        self.assertIn("materiality and stability", text)
        self.assertIn("Do not invent p-values", text)
        self.assertIn("confidence levels", text)
        self.assertIn("significance levels", text)
        self.assertIn("重要性", text)
        self.assertIn("Never write 材料性", text)
        self.assertIn("显著性水平", text)

    def test_boundary_decision_prompt_defines_status_specific_recommendation_shapes(self):
        messages = build_prompt("boundary_decision", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("For low_risk_assumption", text)
        self.assertIn("one non-empty Simplified Chinese business assumption", text)
        self.assertIn("For clear or cannot_answer", text)
        self.assertIn("recommended_assumption as {}", text)
        self.assertIn("Never return recommended_assumption as null", text)
        self.assertIn("Business options must not contain machine ids", text)

    def test_boundary_decision_prompt_separates_scope_from_comparison_baseline(self):
        messages = build_prompt("boundary_decision", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("scope and baseline as separate business axes", text)
        self.assertIn(
            "full_sample must never be used as a comparison baseline",
            text,
        )
        self.assertIn(
            "baseline_binding.confirmed is false",
            text,
        )
        self.assertIn("return needs_question", text)

    def test_boundary_decision_payload_supplies_canonical_baseline_semantics(self):
        valid = {
            "boundary_status": "needs_question",
            "recommended_assumption": {"option": "跟前一天比较"},
            "clarification_questions": [
                {
                    "question": "你希望与哪个时间基准比较？",
                    "options": [
                        "跟前一天比较",
                        "跟近7日均值比较",
                        "跟上周同日比较",
                        "tell the agent to do differently",
                    ],
                }
            ],
            "decision_summary": "比较基准会改变变化方向，需要先确认。",
            "display_summary": "比较基准尚未确定，等待用户选择。",
        }
        client, _ = _provider_client_with_outputs([valid])
        state = {
            "request": {},
            "intent": {
                "question_family": "paid_amount_change_explanation",
                "question_families": ["paid_amount_change_explanation"],
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "pattern_params": {},
                "scope": "full_sample",
                "time_window": "2026-06-01",
                "target_claim": "解释付费金额变化及其驱动因素。",
                "baseline_candidates": [],
                "baseline_binding": {
                    "confirmed": False,
                    "source": "unbound",
                    "candidates": [],
                },
                "claim_intents": ["comparative_change"],
                "ambiguous_slots": [],
            },
            "llm_client": client,
            "llm_calls": [],
        }

        _decide_question_boundary(state)

        prompt_text = state["llm_calls"][0]["messages"][1]["content"]
        self.assertIn('"allowed_baseline_ids"', prompt_text)
        self.assertIn('"allowed_baseline_semantics"', prompt_text)
        self.assertIn('"bound_business_context"', prompt_text)
        self.assertNotIn('"available_defaults"', prompt_text)

    def test_boundary_decision_retries_scope_as_baseline_for_unbound_comparison(self):
        invalid = {
            "boundary_status": "low_risk_assumption",
            "recommended_assumption": {"option": "使用全样本作为基线"},
            "clarification_questions": [],
            "decision_summary": "基线未指定，默认使用全样本。",
            "display_summary": "基线默认采用全样本。",
        }
        valid = {
            "boundary_status": "needs_question",
            "recommended_assumption": {"option": "跟前一天比较"},
            "clarification_questions": [
                {
                    "question": "你希望与哪个时间基准比较？",
                    "options": [
                        "跟前一天比较",
                        "跟近7日均值比较",
                        "跟上周同日比较",
                        "tell the agent to do differently",
                    ],
                }
            ],
            "decision_summary": "比较基准会改变变化方向，需要先确认。",
            "display_summary": "比较基准尚未确定，等待用户选择。",
        }
        client, completions = _provider_client_with_outputs([invalid, valid])
        state = {
            "request": {},
            "intent": {
                "question_family": "paid_amount_change_explanation",
                "question_families": ["paid_amount_change_explanation"],
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "pattern_params": {},
                "scope": "full_sample",
                "time_window": "2026-06-01",
                "target_claim": "解释付费金额变化及其驱动因素。",
                "baseline_candidates": [],
                "baseline_binding": {
                    "confirmed": False,
                    "source": "unbound",
                    "candidates": [],
                },
                "claim_intents": ["comparative_change"],
                "ambiguous_slots": [],
            },
            "llm_client": client,
            "llm_calls": [],
        }

        _decide_question_boundary(state)

        self.assertEqual(completions.attempt_count, 2)
        self.assertEqual(
            state["boundary_decision"]["boundary_status"],
            "needs_question",
        )
        self.assertEqual(
            state["llm_calls"][0]["attempt_failures"][0]["failure_code"],
            "boundary_decision_semantic_invalid:baseline_unbound",
        )

    def test_boundary_decision_retries_cross_field_contract_failure_in_provider(self):
        invalid = {
            "boundary_status": "needs_question",
            "recommended_assumption": {"option": "使用paid_amount继续"},
            "clarification_questions": [
                {
                    "question": "请选择指标。",
                    "options": [
                        "使用paid_amount继续",
                        "选择其他指标",
                        "tell the agent to do differently",
                    ],
                }
            ],
            "decision_summary": "指标选择会影响结论。",
            "display_summary": "等待确认业务指标。",
        }
        valid = {
            **invalid,
            "recommended_assumption": {"option": "使用付费金额继续"},
            "clarification_questions": [
                {
                    "question": "请选择指标。",
                    "options": [
                        "使用付费金额继续",
                        "选择其他指标",
                        "tell the agent to do differently",
                    ],
                }
            ],
        }
        client, completions = _provider_client_with_outputs([invalid, valid])
        state = {
            "request": {},
            "intent": {
                "question_family": "revenue_health_review",
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "scope": "full_sample",
                "time_window": "2026-06-02",
                "baseline_candidates": ["previous_day"],
                "baseline_binding": {
                    "confirmed": True,
                    "source": "request_contract",
                    "candidates": ["previous_day"],
                },
            },
            "llm_client": client,
            "llm_calls": [],
        }

        _decide_question_boundary(state)

        self.assertEqual(completions.attempt_count, 2)
        self.assertEqual(
            state["boundary_decision"]["recommended_assumption"],
            {"option": "使用付费金额继续"},
        )

    def test_boundary_decision_retries_invalid_nonquestion_recommendation_shape(self):
        invalid = {
            "boundary_status": "clear",
            "recommended_assumption": {"option": "沿用当前业务口径继续"},
            "clarification_questions": [],
            "decision_summary": "当前业务边界足够明确。",
            "display_summary": "继续分析。",
        }
        valid = {**invalid, "recommended_assumption": {}}
        client, completions = _provider_client_with_outputs([invalid, valid])
        state = {
            "request": {},
            "intent": {
                "question_family": "revenue_health_review",
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "scope": "full_sample",
                "time_window": "2026-06-02",
                "baseline_candidates": ["previous_day"],
                "baseline_binding": {
                    "confirmed": True,
                    "source": "request_contract",
                    "candidates": ["previous_day"],
                },
            },
            "llm_client": client,
            "llm_calls": [],
        }

        _decide_question_boundary(state)

        self.assertEqual(completions.attempt_count, 2)
        self.assertEqual(state["boundary_decision"]["recommended_assumption"], {})

    def test_confirm_understanding_retries_invalid_typed_shape_in_provider(self):
        invalid = {
            "confirmed_intent": "invalid-string",
            "accepted_assumptions": [],
            "status_message": "已确认业务理解。",
            "display_summary": "已确认业务理解。",
        }
        machine_intent = {
            "question_family": "revenue_health_review",
            "target_metric": "paid_amount",
            "pattern_family": "custom_baseline",
            "scope": "full_sample",
            "time_window": "2026-06-02",
            "target_claim": "检查付费金额经营表现",
            "baseline_candidates": [],
        }
        invalid_assumptions = {
            **invalid,
            "confirmed_intent": {
                "business_summary": "检查六月二日付费金额的经营表现。",
                "machine_intent": machine_intent,
            },
            "accepted_assumptions": "沿用当前业务口径继续。",
        }
        valid = {**invalid_assumptions, "accepted_assumptions": []}
        client, completions = _provider_client_with_outputs(
            [invalid, invalid_assumptions, valid]
        )
        state = {
            "request": {},
            "intent": machine_intent,
            "boundary_decision": {
                "boundary_status": "clear",
                "recommended_assumption": {},
                "clarification_questions": [],
            },
            "llm_client": client,
            "llm_calls": [],
        }

        workflow_module._confirm_business_understanding(state)

        self.assertEqual(completions.attempt_count, 3)
        self.assertEqual(
            state["confirmed_understanding"]["confirmed_intent"]["machine_intent"],
            machine_intent,
        )

    def test_confirm_understanding_copies_ordered_baseline_contract_and_omits_empty_singular_axes(self):
        intent = {
            "question_family": "revenue_health_review",
            "target_metric": "paid_amount",
            "pattern_family": "custom_baseline",
            "scope": "full_sample",
            "time_window": "2026-06-02",
            "target_claim": "检查付费金额经营表现",
            "pattern_params": {},
            "baseline_candidates": [
                "previous_day",
                "rolling_7_day_baseline",
            ],
            "baseline": {},
            "target": {},
        }
        machine_intent = {
            key: deepcopy(value)
            for key, value in intent.items()
            if key not in {"baseline", "target"}
        }
        valid = {
            "confirmed_intent": {
                "business_summary": "确认按两个有序基准核对付费金额经营表现。",
                "machine_intent": machine_intent,
            },
            "accepted_assumptions": [],
            "status_message": "已确认业务理解。",
            "display_summary": "已确认分析边界。",
        }
        client, completions = _provider_client_with_outputs((valid,))
        state = {
            "request": {"run_mode": "production"},
            "intent": intent,
            "boundary_decision": {
                "boundary_status": "clear",
                "recommended_assumption": {},
                "clarification_questions": [],
            },
            "llm_client": client,
            "llm_calls": [],
        }

        workflow_module._confirm_business_understanding(state)

        self.assertEqual(completions.attempt_count, 1)
        confirmed = state["confirmed_understanding"]["confirmed_intent"]
        self.assertEqual(confirmed["machine_intent"], machine_intent)
        user_message = next(
            message
            for message in state["llm_calls"][0]["messages"]
            if message["role"] == "user"
        )["content"]
        payload = json.loads(
            user_message.split("<input_json>", 1)[1]
            .split("</input_json>", 1)[0]
            .strip()
        )
        self.assertEqual(payload["required_machine_intent"], machine_intent)

    def test_confirm_understanding_uses_local_machine_authority_when_provider_mirror_drifts(self):
        intent = {
            "question_family": "revenue_health_review",
            "target_metric": "paid_amount",
            "pattern_family": "custom_baseline",
            "scope": "full_sample",
            "time_window": "2026-06-02",
            "target_claim": "检查付费金额经营表现",
            "pattern_params": {},
            "baseline_candidates": [
                "previous_day",
                "rolling_7_day_baseline",
            ],
            "baseline": {},
            "target": {},
        }
        required = {
            key: deepcopy(value)
            for key, value in intent.items()
            if key not in {"baseline", "target"}
        }

        for boundary, candidates in (
            ("missing", None),
            ("changed", ["same_weekday_baseline", "rolling_7_day_baseline"]),
            ("reordered", ["rolling_7_day_baseline", "previous_day"]),
        ):
            with self.subTest(boundary=boundary):
                machine_intent = deepcopy(required)
                if candidates is None:
                    machine_intent.pop("baseline_candidates")
                else:
                    machine_intent["baseline_candidates"] = candidates
                output = {
                    "confirmed_intent": {
                        "business_summary": "确认按已选基准核对经营表现。",
                        "machine_intent": machine_intent,
                    },
                    "accepted_assumptions": [],
                    "status_message": "已确认业务理解。",
                    "display_summary": "已确认分析边界。",
                }
                normalized = workflow_module._normalize_confirm_understanding_output(
                    output,
                    intent,
                )
                self.assertEqual(
                    normalized["confirmed_intent"]["machine_intent"],
                    required,
                )

    def test_confirm_understanding_requires_exact_nonempty_singular_axes(self):
        intent = {
            "question_family": "custom_baseline_comparison",
            "target_metric": "paid_amount",
            "pattern_family": "custom_baseline",
            "scope": "full_sample",
            "time_window": "2026-06-02",
            "target_claim": "核对目标与基准差异",
            "pattern_params": {},
            "baseline_candidates": ["previous_day"],
            "baseline": {"id": "previous_day"},
            "target": {"id": "target_day"},
        }
        required = deepcopy(intent)
        output = {
            "confirmed_intent": {
                "business_summary": "确认目标日与前一日的比较口径。",
                "machine_intent": required,
            },
            "accepted_assumptions": [],
            "status_message": "已确认业务理解。",
            "display_summary": "已确认分析边界。",
        }

        normalized = workflow_module._normalize_confirm_understanding_output(
            output,
            intent,
        )
        self.assertEqual(
            normalized["confirmed_intent"]["machine_intent"],
            required,
        )
        for field in ("baseline", "target"):
            with self.subTest(field=field):
                drifted = deepcopy(output)
                drifted["confirmed_intent"]["machine_intent"][field] = {
                    "id": "drifted"
                }
                normalized = workflow_module._normalize_confirm_understanding_output(
                    drifted,
                    intent,
                )
                self.assertEqual(
                    normalized["confirmed_intent"]["machine_intent"],
                    required,
                )

    def test_confirm_understanding_prompt_has_stable_business_and_machine_shape(self):
        messages = build_prompt("confirm_understanding", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("confirmed_intent must be a JSON object", text)
        self.assertIn("business_summary", text)
        self.assertIn("machine_intent", text)
        self.assertIn("required_machine_intent", text)
        self.assertIn("do not reinterpret it", text)
        self.assertIn("runtime replaces it with the local contract", text)
        self.assertIn("never a string", text)
        self.assertIn("status_message and accepted_assumptions are shown", text)
        self.assertIn("do not expose internal field names", text)
        self.assertIn("min_periods", text)
        self.assertIn("derive business wording from the supplied structured fields", text)
        self.assertNotIn(
            "全样本、窗口规则、付费金额、重要性和稳定性规则、业务理解已确认",
            text,
        )

    def test_confirm_understanding_prompt_requires_flat_chinese_assumption_array(self):
        messages = build_prompt("confirm_understanding", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("accepted_assumptions must be a flat JSON array", text)
        self.assertIn("use [] when no assumption was accepted", text)
        self.assertIn("never null, an object, or a nested array", text)
        self.assertIn("Machine ids belong only inside confirmed_intent.machine_intent", text)

    def test_analysis_route_prompt_filters_by_supported_question_family(self):
        messages = build_prompt("analysis_route_plan", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("supported_question_families", text)
        self.assertIn("Do not request", text)
        self.assertIn("metric_coverage_profile", text)
        self.assertIn("data_quality_profile", text)
        self.assertIn("weekday_calendar_compare", text)
        self.assertIn("compare_period_phases", text)
        self.assertIn("rolling_window_compare", text)
        self.assertIn("Add formula", text)
        self.assertIn("machine-only", text)

    def test_analysis_route_prompt_separates_machine_ids_from_business_narratives(self):
        plan = build_prompt("analysis_route_plan", {"intent": {}})
        narrative = build_prompt(
            "final_route_narrative",
            {"route_context": {"route_steps": []}},
        )
        plan_text = "\n".join(message["content"] for message in plan.messages)
        narrative_text = "\n".join(
            message["content"] for message in narrative.messages
        )

        self.assertIn("capability ids only in requested_nodes", plan_text)
        self.assertIn("machine-only", plan_text)
        self.assertIn("step_ref", narrative_text)
        self.assertIn("Do not copy or guess capability ids", narrative_text)
        self.assertNotIn("requested_nodes", narrative.required_keys)

    def test_causal_audit_prompt_uses_business_projection_and_advisory_boundary(self):
        spec = build_prompt(
            "causal_audit",
            {"businessContext": {}, "causalReview": {}},
        )
        text = "\n".join(message["content"] for message in spec.messages)

        self.assertIn("Causal Auditor", text)
        self.assertIn("businessContext", text)
        self.assertIn("causalReview", text)
        self.assertIn("accounting contribution", text)
        self.assertIn("does not establish why those components changed", text)
        self.assertIn("causal_assessment", text)
        self.assertIn("plausible_mechanism", text)
        self.assertIn("candidate_hypothesis", text)
        self.assertIn("mixed_or_confounded", text)
        self.assertIn("publishable_wording", text)
        self.assertIn("Simplified Chinese", text)
        self.assertIn("Do not expose hidden chain-of-thought", text)
        self.assertIn("evidence refs", text)
        self.assertIn("provider metadata", text)
        self.assertIn("do not brainstorm candidate mechanisms", text)
        self.assertIn("alternative explanations", text)
        self.assertIn("publishable_wording must explicitly identify", text)
        self.assertIn("evidence classes already supplied in causalReview", text)
        self.assertIn("must not propose a future experiment", text)
        self.assertEqual(
            spec.required_keys,
            (
                "causal_assessment",
                "publishable_wording",
                "supporting_reasons",
                "evidence_limit",
                "display_summary",
            ),
        )
        self.assertNotIn("Analyst draft", text)

    def test_causal_audit_provider_cannot_brainstorm_without_mechanism_evidence(self):
        with self.assertRaisesRegex(
            llm_client_module.LLMOutputError,
            "causal_audit_ungrounded_mechanism",
        ):
            workflow_module._validate_causal_audit_provider_output(
                {
                    "causal_assessment": "not_supported",
                    "publishable_wording": "会计贡献可保留，深层机制未验证。",
                    "supporting_reasons": ["三项会计贡献已经对账。"],
                    "evidence_limit": (
                        "单笔付费金额上升可能受促销活动影响，但尚未验证。"
                    ),
                    "display_summary": "会计贡献可用，深层机制未验证。",
                },
                {
                    "businessContext": {},
                    "causalReview": {
                        "mechanismEvidence": "当前没有独立的对照、时间先后或机制证据。"
                    },
                },
            )

    def test_data_coverage_prompt_uses_result_summary_for_coverage_facts(self):
        messages = build_prompt("data_coverage_interpretation", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("data_result_summary", text)
        self.assertIn("Do not claim complete weeks", text)
        self.assertIn("row_count", text)
        self.assertIn("field_values", text)
        self.assertIn("Keep coverage_status consistent with the narrative", text)
        self.assertIn("do not say the user must confirm", text)
        self.assertIn("sql_hash", text)

    def test_next_action_prompt_uses_business_language_and_stop_rules(self):
        messages = build_prompt("next_action", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("weak_direction", text)
        self.assertIn("below_materiality_floor", text)
        self.assertIn("claim_evidence", text)
        self.assertIn("required claim", text)
        self.assertIn("auxiliary", text)
        self.assertIn("choose synthesize_answer", text)
        self.assertIn("Do not expose", text)
        self.assertIn("internal field names", text)
        self.assertIn("Simplified Chinese", text)

    def test_answer_text_only_clarification_choice_preserves_nonquestion_boundary(self):
        for boundary_status in ("clear", "low_risk_assumption"):
            with self.subTest(boundary_status=boundary_status):
                state = {
                    "request": {
                        "allow_question_interrupt": True,
                        "clarification_choice": {"answer_text": "与前一天比较"},
                    },
                    "checkpoint_events": [{"node": "clarification_policy_gate"}],
                    "intent": {
                        "question_family": "data_quality_or_evidence_review",
                        "target_metric": "paid_amount",
                        "pattern_family": "custom_baseline",
                        "scope": "full_sample",
                        "time_window": "2026-06-02",
                        "ambiguous_slots": [],
                    },
                    "boundary_decision": {
                        "boundary_status": boundary_status,
                        "recommended_assumption": (
                            {"option": "沿用已确认的业务口径继续"}
                            if boundary_status == "low_risk_assumption"
                            else {}
                        ),
                        "clarification_questions": [],
                    },
                }

                _clarification_policy_gate(state)

                self.assertEqual(
                    state["clarification_outcome"]["boundary_status"],
                    boundary_status,
                )
                self.assertEqual(
                    workflow_module._route_after_clarification_policy(state),
                    "confirm",
                )

    def test_needs_question_boundary_still_routes_to_validated_question(self):
        state = {
            "request": {"allow_question_interrupt": True},
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "data_quality_or_evidence_review",
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "scope": "full_sample",
                "time_window": "2026-06-02",
                "ambiguous_slots": [{"slot": "baseline"}],
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": {"option": "与前一天比较"},
                "clarification_questions": [{
                    "question": "请选择对比基准。",
                    "options": [
                        "与前一天比较",
                        "与上周同日比较",
                        "tell the agent to do differently",
                    ],
                }],
            },
            "llm_client": ScriptedLLMClient({
                "clarification_question": {
                    "questions": [{
                        "question": "请选择对比基准。",
                        "options": [
                            "与前一天比较",
                            "与上周同日比较",
                            "tell the agent to do differently",
                        ],
                    }],
                    "recommended_assumption": {"option": "与前一天比较"},
                }
            }),
            "llm_calls": [],
        }

        _clarification_policy_gate(state)
        self.assertEqual(
            workflow_module._route_after_clarification_policy(state),
            "ask",
        )
        workflow_module._generate_clarification(state)

        self.assertEqual(
            len(state["clarification_outcome"]["questions"]),
            1,
        )
        self.assertEqual(state["llm_client"].calls, ["clarification_question"])

    def test_general_clarification_waits_with_validated_business_options(self):
        from bi_agent.runtime.langgraph_workflow import _generate_clarification

        fake = ScriptedLLMClient({
            "clarification_question": {
                "questions": [{
                    "question": "按哪个已确认业务边界继续？",
                    "options": [
                        {"label": "保留当前指标和基线继续。", "description": "保留已确认口径。"},
                        {"label": "调整业务范围后继续。", "description": "用新范围重新绑定。"},
                        "tell the agent to do differently",
                    ],
                }],
                "recommended_assumption": {"option": "保留当前指标和基线继续。"},
                "status_message": "等待确认。",
            }
        })
        with tempfile.TemporaryDirectory() as artifact_root:
            state = {
                "run_id": "run-general-clarification",
                "request": {"artifact_root": artifact_root},
                "intent": {
                    "target_metric": "active_users",
                    "baseline_candidates": ["previous_day"],
                    "scope": "full_sample",
                },
                "boundary_decision": {
                    "boundary_status": "needs_question",
                    "clarification_questions": [],
                },
                "llm_client": fake,
                "llm_calls": [],
                "checkpoint_events": [],
            }

            _generate_clarification(state)
            self.assertEqual(_route_after_clarification(state), "wait")
            _persist_clarification(state)

        self.assertEqual(state["workflow_status"], "waiting_for_clarification")
        self.assertEqual(
            state["answer_package"]["material_slots"]["target_metrics"],
            ["active_users"],
        )
        self.assertEqual(
            state["answer_package"]["material_slots"]["baselines"],
            ["previous_day"],
        )
        self.assertEqual(
            state["answer_package"]["clarification"]["recommended_assumption"],
            {"option": "保留当前指标和基线继续。"},
        )

    def test_general_clarification_option_objects_reject_unsafe_or_ambiguous_shapes(self):
        from bi_agent.runtime.langgraph_workflow import (
            _normalize_general_clarification_output,
        )

        base = {
            "questions": [{
                "question": "按哪个范围继续？",
                "options": [
                    {"label": "保留当前范围", "description": "继续当前口径"},
                    {"label": "调整业务范围", "description": "重新确认口径"},
                    "tell the agent to do differently",
                ],
            }],
            "recommended_assumption": {"label": "保留当前范围"},
        }
        normalized = _normalize_general_clarification_output(base)
        self.assertEqual(
            normalized["questions"][0]["options"][0],
            "保留当前范围",
        )

        invalid_options = (
            {"label": "保留当前范围", "description": "继续", "action": "override"},
            {"label": "", "description": "继续"},
            {"label": "保留当前范围", "description": "evidence_ref"},
        )
        for invalid in invalid_options:
            candidate = {
                **base,
                "questions": [{
                    **base["questions"][0],
                    "options": [
                        invalid,
                        {"label": "调整业务范围", "description": "重新确认"},
                        "tell the agent to do differently",
                    ],
                }],
            }
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                WorkflowFailure,
                "general_clarification_contract_invalid:option_object",
            ):
                _normalize_general_clarification_output(candidate)

        duplicate = {
            **base,
            "questions": [{
                **base["questions"][0],
                "options": [
                    {"label": "保留当前范围", "description": "继续"},
                    {"label": "保留当前范围", "description": "换个说法"},
                    "tell the agent to do differently",
                ],
            }],
        }
        with self.assertRaisesRegex(
            WorkflowFailure,
            "general_clarification_contract_invalid:options",
        ):
            _normalize_general_clarification_output(duplicate)

    def test_general_clarification_rejects_translated_changed_or_reordered_escape(self):
        from bi_agent.runtime.langgraph_workflow import (
            _normalize_general_clarification_output,
        )

        business_options = ["保留当前范围", "调整业务范围"]
        invalid_options = (
            [*business_options, "按其他方式处理"],
            [*business_options, "Tell the agent to do differently"],
            [*business_options, " tell the agent to do differently "],
            ["tell the agent to do differently", *business_options],
        )
        for options in invalid_options:
            with self.subTest(options=options), self.assertRaisesRegex(
                WorkflowFailure,
                "general_clarification_contract_invalid:options",
            ):
                _normalize_general_clarification_output({
                    "questions": [{
                        "question": "按哪个范围继续？",
                        "options": options,
                    }],
                    "recommended_assumption": {"option": business_options[0]},
                })

    def test_general_clarification_prompt_requires_string_option_array(self):
        text = "\n".join(
            message["content"]
            for message in build_prompt("clarification_question", {}).messages
        )

        self.assertIn("options must be an array of strings", text)
        self.assertIn('"options":["业务选项A","业务选项B"', text)

    def test_general_clarification_invalid_recommendation_fails_without_repair_call(self):
        from bi_agent.runtime.langgraph_workflow import _generate_clarification

        fake = ScriptedLLMClient(
            {
                "clarification_question": {
                    "questions": [{
                        "question": "按哪个基线继续？",
                        "options": [
                            "与前一天比较",
                            "与上周同一天比较",
                            "tell the agent to do differently",
                        ],
                    }],
                    "recommended_assumption": {"option": "使用推荐基线"},
                },
            }
        )
        state = {
            "request": {},
            "intent": {"target_metric": "active_users"},
            "boundary_decision": {
                "boundary_status": "needs_question",
                "clarification_questions": [],
            },
            "llm_client": fake,
            "llm_calls": [],
            "checkpoint_events": [],
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            "general_clarification_contract_invalid:recommended_option",
        ):
            _generate_clarification(state)

        self.assertEqual(fake.calls, ["clarification_question"])

    def test_route_requirements_preserve_confirmed_material_slots_and_flag_conflict(self):
        state = {
            "intent": {
                "target_metric": "active_users",
                "baseline_candidates": ["previous_day"],
                "context_sources": ["external_event"],
                "scope": "full_sample",
            },
            "request": {},
        }
        merged, conflicts = _merge_confirmed_material_requirements(
            {
                "analysis_requirements": {
                    "target_metrics": [],
                    "baselines": [],
                    "context_sources": [],
                    "scope": "full_sample",
                }
            },
            state,
        )

        self.assertEqual(merged["analysis_requirements"]["target_metrics"], ["active_users"])
        self.assertEqual(merged["analysis_requirements"]["baselines"], ["previous_day"])
        self.assertEqual(merged["analysis_requirements"]["context_sources"], ["external_event"])
        self.assertEqual(conflicts, ())

        _, conflicts = _merge_confirmed_material_requirements(
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "scope": "channel",
                }
            },
            state,
        )
        self.assertEqual(set(conflicts), {"target_metrics", "scope"})

    def test_route_requirements_treat_equivalent_scope_token_shapes_as_same_material(self):
        state = {
            "intent": {
                "target_metric": "paid_amount",
                "scope": "full_sample",
            },
            "request": {},
        }
        merged, conflicts = _merge_confirmed_material_requirements(
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "scope": {"type": "full_sample"},
                }
            },
            state,
        )

        self.assertEqual(conflicts, ())
        self.assertEqual(
            merged["analysis_requirements"]["scope"],
            "full_sample",
        )
        _, all_users_conflicts = _merge_confirmed_material_requirements(
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "scope": {"type": "full_sample"},
                }
            },
            {
                "intent": {
                    "target_metric": "paid_amount",
                    "scope": "all_users",
                },
                "request": {},
            },
        )
        self.assertEqual(all_users_conflicts, ())
        _, material_conflicts = _merge_confirmed_material_requirements(
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "scope": {
                        "type": "full_sample",
                        "segment": "high_value_users",
                    },
                }
            },
            state,
        )
        self.assertEqual(material_conflicts, ("scope",))



    def test_invalid_general_clarification_fails_after_one_node_call(self):
        from bi_agent.runtime.langgraph_workflow import _generate_clarification

        class InvalidThenValid(ScriptedLLMClient):
            def __init__(self):
                super().__init__({})
                self.attempts = 0

            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                if task != "clarification_question":
                    return super().invoke_json(
                        task=task,
                        prompt_version=prompt_version,
                        messages=messages,
                        required_keys=required_keys,
                    )
                self.attempts += 1
                options = (
                    ["只有一个业务选项", "tell the agent to do differently"]
                    if self.attempts == 1
                    else [
                        "保留当前指标和基线继续。",
                        "调整业务范围后继续。",
                        "tell the agent to do differently",
                    ]
                )
                return ScriptedLLMResult(
                    {
                        "questions": [{"question": "按哪个范围继续？", "options": options}],
                        "recommended_assumption": {
                            "option": "保留当前指标和基线继续。"
                        },
                        "status_message": "等待确认。",
                    },
                    {"task": task},
                )

        fake = InvalidThenValid()
        state = {
            "run_id": "run-general-retry",
            "request": {},
            "intent": {"target_metric": "active_users"},
            "boundary_decision": {
                "boundary_status": "needs_question",
                "clarification_questions": [],
            },
            "llm_client": fake,
            "llm_calls": [],
            "checkpoint_events": [],
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            "general_clarification_contract_invalid:options",
        ):
            _retrying_node("generate_clarification", _generate_clarification)(state)

        self.assertEqual(fake.attempts, 1)

    def test_general_clarification_contract_has_no_business_layer_retry(self):
        from bi_agent.runtime.langgraph_workflow import _generate_clarification

        state = {
            "run_id": "run-general-three-attempts",
            "request": {},
            "intent": {"target_metric": "paid_amount"},
            "boundary_decision": {
                "boundary_status": "needs_question",
                "clarification_questions": [],
            },
            "next_action": {"next_action": "ask_question"},
            "checkpoint_events": [],
        }
        payloads = []

        def clarify(_state, task, payload):
            self.assertEqual(task, "clarification_question")
            payloads.append(payload)
            options = (
                ["只有一个业务选项", "tell the agent to do differently"]
                if len(payloads) < 3
                else [
                    "保留当前口径继续",
                    "调整业务口径后继续",
                    "tell the agent to do differently",
                ]
            )
            return {
                "questions": [{"question": "按哪个口径继续？", "options": options}],
                "recommended_assumption": {"option": "保留当前口径继续"},
            }

        with patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
            side_effect=clarify,
        ):
            with self.assertRaisesRegex(
                WorkflowFailure,
                "general_clarification_contract_invalid:options",
            ):
                _retrying_node(
                    "generate_clarification",
                    _generate_clarification,
                )(state)

        self.assertEqual(len(payloads), 1)

    def test_segment_causal_followup_defaults_to_observational_boundary(self):
        state = {
            "request": {"allow_question_interrupt": True},
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "segment_or_factor_attribution",
                "primary_question_family": "segment_or_factor_attribution",
                "question_families": ["segment_or_factor_attribution"],
                "secondary_question_families": [],
                "question": "这些渠道里 WajeSpecial 是主要原因吗？",
                "target_claim": "判断 WajeSpecial 是否是主要原因",
                "ambiguous_slots": [{"slot": "claim_strength"}],
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01..2026-05",
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": "",
                "clarification_questions": [
                    {"question": "是否允许把 WajeSpecial 写成主要原因？"},
                ],
            },
        }

        _clarification_policy_gate(state)

        self.assertEqual(
            state["clarification_outcome"]["boundary_status"],
            "low_risk_assumption",
        )
        self.assertIn("观察性归因", state["clarification_outcome"]["recommended_assumption"])

    def test_unregistered_clarification_choice_cannot_authorize_continue(self):
        state = {
            "request": {
                "allow_question_interrupt": True,
                "clarification_choice": {
                    "answer_text": "按日粒度，移除贡献最大的正向日期后复算。",
                    "outlier_removal_strategy": "daily_remove_top_positive_day",
                },
            },
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "custom_baseline_comparison",
                "primary_question_family": "custom_baseline_comparison",
                "question": "按日粒度，移除贡献最大的正向日期后复算。",
                "ambiguous_slots": [{"slot": "baseline"}],
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01..2026-05",
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": "",
                "clarification_questions": [
                    {"question": "请确认异常日期移除规则。"},
                ],
            },
        }

        _clarification_policy_gate(state)

        self.assertEqual(
            state["clarification_outcome"]["boundary_status"],
            "needs_question",
        )
        self.assertEqual(
            workflow_module._route_after_clarification_policy(state),
            "ask",
        )
        self.assertFalse(state["clarification_outcome"]["choice"])

    def test_material_clarification_choice_rebinds_typed_axis_before_confirm(self):
        state = {
            "request": {
                "allow_question_interrupt": True,
                "clarification_choice": {
                    "pattern_family": "weekly",
                    "pattern_params": {"target_weekday": 2},
                },
            },
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "pattern_explanation",
                "question_families": ["pattern_explanation"],
                "target_metric": "paid_amount",
                "pattern_family": "rolling",
                "pattern_params": {},
                "scope": "full_sample",
                "time_window": "2026-05-26..2026-06-02",
                "target_claim": "核对重复时间形态",
                "baseline_candidates": [],
                "ambiguous_slots": ["pattern_family", "scope"],
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": {"option": "采用已选择的时间形态继续"},
                "clarification_questions": [
                    {"question": "请选择时间形态。"}
                ],
            },
        }

        _clarification_policy_gate(state)

        self.assertEqual(
            workflow_module._route_after_clarification_policy(state),
            "rebind",
        )
        workflow_module._rebind_after_clarification(state)
        self.assertEqual(state["intent"]["pattern_family"], "weekly")
        self.assertEqual(
            state["intent"]["pattern_params"],
            {"target_weekday": 2},
        )
        self.assertEqual(state["intent"]["ambiguous_slots"], ["scope"])
        state["boundary_decision"] = {
            "boundary_status": "needs_question",
            "recommended_assumption": {"option": "请选择剩余业务范围"},
            "clarification_questions": [{"question": "请选择业务范围。"}],
        }

        _clarification_policy_gate(state)

        self.assertEqual(
            workflow_module._route_after_clarification_policy(state),
            "ask",
        )
        self.assertFalse(state["clarification_outcome"]["choice"])

    def test_untyped_or_signed_axis_drifting_clarification_choice_stays_in_clarification(self):
        base_state = {
            "request": {"allow_question_interrupt": True},
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "pattern_explanation",
                "question_families": ["pattern_explanation"],
                "target_metric": "paid_amount",
                "pattern_family": "rolling",
                "pattern_params": {},
                "scope": "full_sample",
                "time_window": "2026-05-26..2026-06-02",
                "target_claim": "核对重复时间形态",
                "baseline_candidates": [],
                "ambiguous_slots": ["pattern_family"],
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": {"option": "采用已选择的时间形态继续"},
                "clarification_questions": [
                    {"question": "请选择时间形态。"}
                ],
            },
        }
        choices = {
            "untyped": {"answer_text": "采用用户选择的业务口径。"},
            "signed_axis_drift": {"target_metric": "active_users"},
            "unknown_typed_value": {"pattern_family": "invented_pattern"},
            "noncanonical_alias": {"question_families": ["pattern_explanation"]},
            "unknown_nonmaterial_bypass": {"unreviewed_choice": "continue"},
        }

        for boundary, choice in choices.items():
            with self.subTest(boundary=boundary):
                state = deepcopy(base_state)
                state["request"]["clarification_choice"] = choice

                _clarification_policy_gate(state)

                self.assertEqual(
                    state["clarification_outcome"]["boundary_status"],
                    "needs_question",
                )
                self.assertEqual(
                    workflow_module._route_after_clarification_policy(state),
                    "ask",
                )
                self.assertFalse(state["clarification_outcome"]["choice"])

    def test_clarification_choice_requires_valid_typed_authority_before_rebind(self):
        clarification_output = {
            "questions": [
                {
                    "question": "请选择对比基准。",
                    "options": [
                        "与前一天比较",
                        "与上周同日比较",
                        "tell the agent to do differently",
                    ],
                }
            ],
            "recommended_assumption": {"option": "与前一天比较"},
        }
        base_state = {
            "request": {"allow_question_interrupt": True},
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "custom_baseline_comparison",
                "question_families": ["custom_baseline_comparison"],
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "pattern_params": {},
                "scope": "full_sample",
                "time_window": "2026-06-02",
                "target_claim": "核对目标日相对基准的变化",
                "baseline_candidates": [],
                "ambiguous_slots": ["baseline"],
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": {"option": "与前一天比较"},
                "clarification_questions": clarification_output["questions"],
            },
            "llm_client": ScriptedLLMClient(
                {"clarification_question": clarification_output}
            ),
            "llm_calls": [],
        }
        waiting_choices = (
            {"answer_text": "与前一天比较"},
            {
                "answer_text": "与前一天比较",
                "baseline_candidates": ["invented_baseline"],
            },
        )

        for choice in waiting_choices:
            with self.subTest(choice=choice):
                state = deepcopy(base_state)
                state["request"]["clarification_choice"] = choice

                _clarification_policy_gate(state)
                self.assertEqual(
                    workflow_module._route_after_clarification_policy(state),
                    "ask",
                )
                workflow_module._generate_clarification(state)

                self.assertFalse(state["clarification_outcome"]["choice"])
                self.assertEqual(
                    state["clarification_outcome"]["display_answer_text"],
                    "与前一天比较",
                )
                self.assertEqual(
                    workflow_module._route_after_clarification(state),
                    "wait",
                )
                self.assertEqual(state["intent"]["ambiguous_slots"], ["baseline"])

        state = deepcopy(base_state)
        state["request"]["clarification_choice"] = {
            "answer_text": "与前一天比较",
            "baseline_candidates": ["previous_day"],
        }

        _clarification_policy_gate(state)

        self.assertEqual(
            workflow_module._route_after_clarification_policy(state),
            "rebind",
        )
        self.assertEqual(
            state["clarification_outcome"]["choice"],
            {"baseline_candidates": ["previous_day"]},
        )
        self.assertEqual(
            state["clarification_outcome"]["display_answer_text"],
            "与前一天比较",
        )
        workflow_module._rebind_after_clarification(state)
        self.assertEqual(state["intent"]["baseline_candidates"], ["previous_day"])
        self.assertEqual(state["intent"]["ambiguous_slots"], [])

    def test_segment_followup_defaults_to_current_topic_boundary(self):
        state = {
            "request": {
                "allow_question_interrupt": True,
                "context_manifest": {
                    "items": [{"source_type": "topic", "source_ref": "topic-1"}]
                },
            },
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "segment_or_factor_attribution",
                "primary_question_family": "segment_or_factor_attribution",
                "question_families": ["segment_or_factor_attribution"],
                "question": "这些变化在哪些渠道最明显？",
                "target_claim": "识别渠道变化最明显的贡献项",
                "ambiguous_slots": [{"slot": "baseline"}, {"slot": "change_measure"}],
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01..2026-05",
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": "",
                "clarification_questions": [{"question": "请确认变化基线。"}],
            },
        }

        _clarification_policy_gate(state)

        self.assertEqual(
            state["clarification_outcome"]["boundary_status"],
            "low_risk_assumption",
        )
        self.assertIn("当前 topic", state["clarification_outcome"]["recommended_assumption"])

    def test_grain_correction_defaults_to_current_topic_boundary(self):
        state = {
            "request": {
                "allow_question_interrupt": True,
                "context_manifest": {
                    "items": [{"source_type": "topic", "source_ref": "topic-1"}]
                },
            },
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "pattern_explanation",
                "primary_question_family": "pattern_explanation",
                "question": "口径改成按周看，还一样吗？",
                "target_claim": "按周粒度复核付费金额方向",
                "ambiguous_slots": [{"slot": "grain"}],
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01..2026-05",
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": "",
                "clarification_questions": [{"question": "请确认原口径。"}],
            },
        }

        _clarification_policy_gate(state)

        self.assertEqual(
            state["clarification_outcome"]["boundary_status"],
            "low_risk_assumption",
        )
        self.assertIn("按周", state["clarification_outcome"]["recommended_assumption"])

    def test_daily_average_correction_defaults_to_current_topic_boundary(self):
        state = {
            "request": {
                "allow_question_interrupt": True,
                "context_manifest": {
                    "items": [{"source_type": "topic", "source_ref": "topic-1"}]
                },
            },
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "custom_baseline_comparison",
                "primary_question_family": "custom_baseline_comparison",
                "question": "换成日均再看一遍。",
                "target_claim": "按日均付费金额重新比较",
                "ambiguous_slots": [{"slot": "baseline"}],
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01..2026-05",
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": "",
                "clarification_questions": [{"question": "请确认日均基准。"}],
            },
        }

        _clarification_policy_gate(state)

        self.assertEqual(
            state["clarification_outcome"]["boundary_status"],
            "low_risk_assumption",
        )
        self.assertIn("日均", state["clarification_outcome"]["recommended_assumption"])

    def test_actionability_challenge_defaults_to_current_topic_boundary(self):
        state = {
            "request": {
                "allow_question_interrupt": True,
                "context_manifest": {
                    "items": [{"source_type": "topic", "source_ref": "topic-1"}]
                },
            },
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "business_object_impact_review",
                "primary_question_family": "business_object_impact_review",
                "question": "这个结果能不能直接指导投放？",
                "target_claim": "判断当前结果能否直接指导投放",
                "ambiguous_slots": [{"slot": "action_scope"}],
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01..2026-05",
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": "",
                "clarification_questions": [{"question": "请确认投放口径。"}],
            },
        }

        _clarification_policy_gate(state)

        self.assertEqual(
            state["clarification_outcome"]["boundary_status"],
            "low_risk_assumption",
        )
        self.assertIn("指导投放", state["clarification_outcome"]["recommended_assumption"])

    def test_evidence_interpretation_prompt_keeps_boundary_business_readable(self):
        messages = build_prompt("evidence_interpretation", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("evidence_boundary must be a string", text)
        self.assertIn("Do not return an object", text)
        self.assertIn("Do not expose capability ids", text)
        self.assertIn("do not call the result stable or reliable", text)
        self.assertIn("write a separate sentence for each factor state", text)
        self.assertIn("Never join factors with different states", text)
        self.assertIn("Simplified Chinese", text)

    def test_answer_synthesis_prompt_uses_business_projection_and_local_claim_authority(self):
        messages = build_prompt(
            "answer_synthesis",
            {"businessContext": {}},
        ).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("read-only businessContext", text)
        self.assertIn("claimSlots", text)
        self.assertIn("factorStates", text)
        self.assertIn("unavailableConclusions", text)
        self.assertIn("must not erase the ranking", text)
        self.assertIn("never return claims", text)
        self.assertNotIn("auxiliary_limitation_scopes", text)
        self.assertNotIn("missing_formula_dimension", text)

    def test_answer_draft_projects_used_narrative_and_ignores_owner_metadata(self):
        output = workflow_module._normalized_answer_draft_provider_output(
            {
                "answer_text": (
                    "WajeSpecial 渠道和 Samsung 设备是当前增量的重要落点，"
                    "Spearman 结果仅作辅助参考。"
                ),
                "display_summary": "已完成渠道和设备定位。",
                "owner": "workflow_owner",
            }
        )

        self.assertEqual(
            set(output),
            {"answer_text", "display_summary"},
        )
        self.assertIn("WajeSpecial", output["answer_text"])

    def test_answer_draft_rejects_provider_owned_claims(self):
        with self.assertRaisesRegex(
            LLMOutputError,
            "answer_synthesis_returned_canonical_claims",
        ):
            workflow_module._validate_answer_draft_provider_output(
                {
                    "answer_text": "当前答案有本地证据支持。",
                    "claims": [{"text": "由模型声明的结论"}],
                }
            )

    def test_answer_draft_business_entities_reach_statement_semantic_audit(self):
        state = _required_claim_resolution_state()
        state.update(
            {
                "run_id": "answer-business-entity-semantic-audit",
                "llm_client": ScriptedLLMClient(
                    {
                        "answer_synthesis": {
                            "answer_text": (
                                "WajeSpecial 渠道和 Samsung 设备承接了全部增长，"
                                "Spearman 结果证明了业务原因。"
                            ),
                            "display_summary": "已形成业务答案。",
                        },
                        "semantic_audit": {
                            "audit_status": "needs_revision",
                            "issues": [
                                {
                                    "severity": "error",
                                    "description": (
                                        "全部增长和证明业务原因的措辞超出当前证据。"
                                    ),
                                }
                            ],
                        },
                    }
                ),
                "llm_calls": [],
                "validator_results": [],
            }
        )
        _reduce_evidence(state)

        workflow_module._synthesize_answer(state)
        workflow_module._semantic_audit(state)

        self.assertIn("WajeSpecial", state["answer_text"])
        self.assertEqual(
            state["retry_context"]["failure_type"],
            "semantic_audit",
        )

    def test_semantic_audit_prompt_keeps_issue_descriptions_business_readable(self):
        spec = build_prompt("semantic_audit", {"answer_text": "check"})
        text = "\n".join(message["content"] for message in spec.messages)

        self.assertIn("Issue descriptions", text)
        self.assertIn("business-readable Chinese", text)
        self.assertIn("Do not expose internal field names", text)
        self.assertIn("draft_claims", text)
        self.assertIn("evidence_brief", text)
        self.assertIn("wording_limit", text)
        self.assertNotIn("extracted_claims", spec.required_keys)
        self.assertIn("Do not return extracted_claims", text)
        self.assertIn("Chinese quotation marks", text)

    def test_semantic_audit_receives_business_projection_only(self):
        state = _required_claim_resolution_state()
        fake = ScriptedLLMClient(
            {"semantic_audit": {"audit_status": "passed", "issues": []}}
        )
        state.update(
            {
                "run_id": "semantic-audit-business-projection",
                "llm_client": fake,
                "llm_calls": [],
                "answer_text": "已按业务证据说明付费金额变化和因素贡献。",
                "validator_results": [],
            }
        )
        _reduce_evidence(state)
        state["draft_claims"] = workflow_module._authority_claims_from_evidence(state)

        workflow_module._semantic_audit(state)

        payload = _llm_input_payload(
            {"admin_audit": {"llm_calls": state["llm_calls"]}},
            "semantic_audit",
        )
        self.assertEqual(
            set(payload),
            {"answerText", "businessContext", "displayReview"},
        )
        visible = json.dumps(payload, ensure_ascii=False)
        self.assertIn("付费金额", visible)
        for internal in (
            "evidence_ref",
            "capability_id",
            "draft_claims",
            "evidence_brief",
            "driver_decomposition",
            "formula_component_contribution",
        ):
            self.assertNotIn(internal, visible)

    def test_answer_prompts_remove_unlisted_claims_and_action_advice(self):
        for task in ("answer_synthesis", "answer_repair"):
            messages = build_prompt(task, {"answer_context": {}}).messages
            text = "\n".join(message["content"] for message in messages)

            self.assertIn("unlisted claims", text)
            self.assertRegex(text, r"remove (?:it|them) from answer_text")
            self.assertIn("Do not add operational action recommendations", text)

    def test_answer_repair_prompt_uses_business_review_without_runtime_diagnostics(self):
        messages = build_prompt(
            "answer_repair",
            {
                "answerText": "待修正文案",
                "businessContext": {},
                "displayReview": {},
            },
        ).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("answerText", text)
        self.assertIn("businessContext", text)
        self.assertIn("displayReview", text)
        self.assertIn("read-only local authority", text)
        self.assertNotIn("retry_context", text)
        self.assertNotIn("failure_reason", text)

    def test_answer_prompts_block_customer_metadata_leaks(self):
        for task in ("answer_synthesis", "answer_repair"):
            messages = build_prompt(task, {"answer_context": {}}).messages
            text = "\n".join(message["content"] for message in messages)

            self.assertIn("raw SQL", text)
            self.assertIn("internal ids", text)
            self.assertIn("enum tokens", text)
            self.assertIn("evidence refs", text)
            self.assertIn("provider metadata", text)

    def test_degraded_prompt_explains_materiality_without_data_volume_drift(self):
        messages = build_prompt("degraded_explanation", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("below_materiality_floor", text)
        self.assertIn("change size", text)
        self.assertIn("not data volume", text)
        self.assertIn("weak_direction", text)
        self.assertIn("fewer valid comparable periods than the run requires", text)
        self.assertIn("Do not suggest changing, adjusting, or relaxing thresholds", text)

    def test_degraded_explanation_receives_fixed_analysis_contract_windows(self):
        from bi_agent.runtime.langgraph_workflow import _generate_degraded_explanation

        contract = {
            "as_of": "2026-06-03T12:00:00+01:00",
            "resolved_windows": [
                {"window_id": "target_day", "label": "2026-06-02"},
                {"window_id": "previous_day", "label": "2026-06-01"},
            ],
        }
        state = {
            "request": {"run_mode": "production", "analysis_contract": contract},
            "run_id": "fixed-degraded-window",
            "intent": {
                "scope": "full_sample",
                "time_window": "yesterday",
                "target_metric": "paid_amount",
            },
            "evidence_brief": {},
            "verifier": {},
            "evidence": [],
            "draft_claims": [],
        }
        captured = {}

        def explain(_state, task, payload, **_kwargs):
            self.assertEqual(task, "degraded_explanation")
            captured.update(payload)
            return {
                "status": "degraded",
                "explanation": "固定目标日的数据源尚未绑定。",
                "repair_path": "注册数据集快照后重试。",
            }

        with patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
            side_effect=explain,
        ):
            _generate_degraded_explanation(state)

        self.assertEqual(captured["analysis_contract"], contract)

    def test_final_answer_audit_prompt_keeps_provider_review_advisory(self):
        spec = build_prompt("final_answer_audit", {"final_answer": "check"})
        text = "\n".join(message["content"] for message in spec.messages)

        self.assertIn("cannot grant or revoke claim authority", text)
        self.assertIn("unsupported_material_claim", text)
        self.assertIn("exact non-empty substring of finalAnswer", text)
        self.assertIn("businessContext.reviewAnchors", text)
        self.assertIn("remove, weaken, or clarify", text)
        self.assertIn("Do not invent or request hypothetical business causes", text)
        self.assertIn("Return exactly one top-level field: material_findings", text)
        self.assertIn("现有业务证据", text)
        self.assertEqual(
            spec.required_keys,
            ("material_findings",),
        )
        self.assertNotIn("display_status", spec.required_keys)
        self.assertNotIn("hard_blockers", spec.required_keys)
        self.assertNotIn("retry_instruction", spec.required_keys)

    def test_final_answer_audit_provider_finding_requires_exact_answer_excerpt(self):
        final_answer = (
            "最终结论：单笔付费金额是主要正向贡献项，"
            "付费频次形成负向抵消，付费人数提供小幅正向贡献。"
        )

        with self.assertRaisesRegex(
            llm_client_module.LLMOutputError,
            "final_answer_audit_excerpt_not_found",
        ):
            workflow_module._validate_final_answer_audit_provider_output(
                {
                    "material_findings": [
                        {
                            "code": "claim_paraphrase_unclear",
                            "answer_excerpt": "答案缺少主要贡献项",
                            "context_anchor": {
                                "kind": "claim_slot",
                                "key": "结论1",
                            },
                            "edit_action": "clarify",
                            "explanation": "这段表达没有突出主要贡献项。",
                        }
                    ],
                },
                final_answer=final_answer,
                business_context={
                    "reviewAnchors": [
                        {
                            "kind": "claim_slot",
                            "key": "结论1",
                            "summary": "单笔付费金额是主要贡献项。",
                        }
                    ],
                },
            )

    def test_final_answer_audit_provider_cannot_add_hypothetical_causes(self):
        with self.assertRaisesRegex(
            llm_client_module.LLMOutputError,
            "final_answer_audit_invents_business_cause",
        ):
            workflow_module._validate_final_answer_audit_provider_output(
                {
                    "material_findings": [
                        {
                            "code": "claim_paraphrase_unclear",
                            "answer_excerpt": "单笔付费金额是主要贡献项",
                            "context_anchor": {
                                "kind": "boundary",
                                "key": "原因边界",
                            },
                            "edit_action": "clarify",
                            "explanation": "可能受高客单价商品或活动影响。",
                        }
                    ],
                },
                final_answer="最终结论：单笔付费金额是主要贡献项。",
                business_context={
                    "reviewAnchors": [
                        {
                            "kind": "boundary",
                            "key": "原因边界",
                            "summary": "深层业务机制需要独立证据。",
                        }
                    ],
                },
            )

    def test_final_answer_audit_specific_cause_requires_boundary_anchor(self):
        with self.assertRaisesRegex(
            llm_client_module.LLMOutputError,
            "final_answer_audit_cause_anchor_invalid",
        ):
            workflow_module._validate_final_answer_audit_provider_output(
                {
                    "material_findings": [
                        {
                            "code": "unsupported_material_claim",
                            "answer_excerpt": "促销活动导致单笔付费金额上升",
                            "context_anchor": {
                                "kind": "factor_state",
                                "key": "单笔付费金额",
                            },
                            "edit_action": "remove",
                            "explanation": "现有业务证据没有验证该具体原因。",
                        }
                    ]
                },
                final_answer="最终结论：促销活动导致单笔付费金额上升。",
                business_context={
                    "reviewAnchors": [
                        {
                            "kind": "factor_state",
                            "key": "单笔付费金额",
                            "summary": "已量化贡献",
                        },
                        {
                            "kind": "boundary",
                            "key": "原因边界",
                            "summary": "具体业务原因需要独立证据。",
                        },
                    ]
                },
            )

    def test_final_answer_audit_cannot_reject_exact_verified_values(self):
        final_answer = (
            "2026年6月1日付费金额为308,240,309.0，"
            "较2026年5月31日的304,142,630.0上涨1.35%，"
            "增加4,097,679.0。"
        )
        with self.assertRaisesRegex(
            llm_client_module.LLMOutputError,
            "final_answer_audit_finding_contradicts_verified_value",
        ):
            workflow_module._validate_final_answer_audit_provider_output(
                {
                    "material_findings": [
                        {
                            "code": "unsupported_material_claim",
                            "answer_excerpt": final_answer,
                            "context_anchor": {
                                "kind": "claim_slot",
                                "key": "结论1",
                            },
                            "edit_action": "weaken",
                            "explanation": (
                                "现有业务证据只提供近似值，"
                                "不支持答案中的精确数字。"
                            ),
                        }
                    ]
                },
                final_answer=final_answer,
                business_context={
                    "reviewAnchors": [
                        {
                            "kind": "claim_slot",
                            "key": "结论1",
                            "summary": "付费金额从3.0414亿增至3.0824亿。",
                        },
                        {
                            "kind": "verified_fact",
                            "key": "精确对比值1",
                            "summary": final_answer,
                        },
                    ]
                },
            )

    def test_degraded_explanation_sanitizes_unsupported_period_and_threshold_advice(self):
        with self.assertRaisesRegex(WorkflowFailure, "materiality_drift"):
            _sanitize_terminal_explanation(
                {
                    "status": "degraded",
                    "explanation": "变化幅度低于重要性阈值，同时可比较期间数量不足，无法确认模式。",
                    "repair_path": "建议调整重要性阈值，或扩大时间窗口。",
                },
                {
                    "evidence_brief": {
                        "limitations": ["below_materiality_floor", "weak_direction"],
                    }
                },
                "degraded",
            )

    def test_degraded_explanation_rejects_invented_future_window(self):
        with self.assertRaisesRegex(WorkflowFailure, "repair_path_future_window"):
            _sanitize_terminal_explanation(
                {
                    "status": "degraded",
                    "explanation": "方向不一致且变化幅度低于当前重要性阈值。",
                    "repair_path": "延长观察周期至12个月以上后重新评估。",
                },
                {
                    "evidence_brief": {
                        "limitations": ["below_materiality_floor", "weak_direction"],
                    }
                },
                "degraded",
            )

    def test_degraded_explanation_sanitizes_contract_and_data_collection_drift(self):
        with self.assertRaisesRegex(WorkflowFailure, "data_or_contract_drift"):
            _sanitize_terminal_explanation(
                {
                    "status": "degraded",
                    "explanation": "未发现明确的事件或合同依据，因此模式无法确认。",
                    "repair_path": "建议收集更多数据并积累更多月度数据。",
                },
                {
                    "evidence_brief": {
                        "limitations": [
                            "below_materiality_floor",
                            "weak_direction",
                            "no_event_contract_or_matches",
                        ],
                    }
                },
                "degraded",
            )

    def test_fixed_future_window_detection_does_not_flag_calendar_dates(self):
        self.assertFalse(
            _repair_path_invents_fixed_future_window(
                "观察窗口覆盖2026年1月1日至6月30日，结论只适用于当前窗口。"
            )
        )
        self.assertFalse(_repair_path_invents_fixed_future_window("仅10%（3个月）支持该假设。"))
        self.assertTrue(_repair_path_invents_fixed_future_window("延长观察周期至12个月以上后重新评估。"))

    def test_missing_llm_env_fails_before_claiming_draft(self):
        with self.assertRaisesRegex(LLMConfigurationError, "missing_llm_model"):
            OpenAICompatibleLLMClient.from_env({})

    def test_llm_client_waits_indefinitely_by_default(self):
        client = OpenAICompatibleLLMClient.from_env(
            {
                "WAJE_LLM_PROVIDER": "openai_compatible",
                "WAJE_LLM_MODEL": "patient-model",
                "WAJE_LLM_API_KEY": "test-key",
            }
        )

        self.assertIsNone(client.timeout_seconds)

    def test_llm_client_accepts_disabled_timeout_env(self):
        for timeout_value in ("0", "none", "disabled", ""):
            with self.subTest(timeout_value=timeout_value):
                client = OpenAICompatibleLLMClient.from_env(
                    {
                        "WAJE_LLM_PROVIDER": "openai_compatible",
                        "WAJE_LLM_MODEL": "patient-model",
                        "WAJE_LLM_API_KEY": "test-key",
                        "WAJE_LLM_TIMEOUT_SECONDS": timeout_value,
                    }
                )

                self.assertIsNone(client.timeout_seconds)

    def test_llm_client_enforces_wall_clock_timeout(self):
        class SlowCompletions:
            def create(self, **kwargs):
                time.sleep(0.2)

        class SlowChat:
            completions = SlowCompletions()

        class SlowClient:
            chat = SlowChat()

        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="slow-model",
            api_key="test-key",
            timeout_seconds=0.01,
        )
        client._client = SlowClient()

        with self.assertRaisesRegex(LLMTimeoutError, "llm_request_timeout"):
            client.invoke_json(
                task="business_intent",
                prompt_version="test",
                messages=[{"role": "user", "content": "{}"}],
                required_keys=[],
            )

    def test_llm_client_retries_transient_failures_three_times(self):
        attempts = {"count": 0}

        class ResponseMessage:
            content = '{"ok": true}'

        class ResponseChoice:
            message = ResponseMessage()

        class Response:
            id = "response-retry-success"
            choices = [ResponseChoice()]
            usage = None

        class FlakyCompletions:
            def create(self, **kwargs):
                attempts["count"] += 1
                if attempts["count"] < 3:
                    raise RuntimeError("temporary-network-error")
                return Response()

        class FlakyChat:
            completions = FlakyCompletions()

        class FlakyClient:
            chat = FlakyChat()

        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="retry-model",
            api_key="test-key",
        )
        client._client = FlakyClient()

        result = client.invoke_json(
            task="business_intent",
            prompt_version="test",
            messages=[{"role": "user", "content": "{}"}],
            required_keys=["ok"],
        )

        self.assertEqual(result.output["ok"], True)
        self.assertEqual(attempts["count"], 3)
        self.assertEqual(result.audit["attempt_count"], 3)

    def test_llm_client_disables_sdk_retries_for_each_outer_attempt(self):
        constructor_calls = []

        class ResponseMessage:
            content = '{"ok": true}'

        class ResponseChoice:
            message = ResponseMessage()

        class Response:
            id = "response-no-inner-retry"
            choices = [ResponseChoice()]
            usage = None

        class FakeCompletions:
            def create(self, **_kwargs):
                return Response()

        class FakeChat:
            completions = FakeCompletions()

        class FakeOpenAI:
            def __init__(self, **kwargs):
                constructor_calls.append(dict(kwargs))
                self.chat = FakeChat()

        with patch.object(llm_client_module, "OpenAI", FakeOpenAI):
            OpenAICompatibleLLMClient(
                provider="openai_compatible",
                model="outer-retry-model",
                api_key="test-key",
            )
            llm_client_module._request_openai_json_once(
                {
                    "api_key": "test-key",
                    "base_url": "",
                    "timeout_seconds": None,
                    "model": "outer-retry-model",
                },
                [{"role": "user", "content": "{}"}],
            )

        self.assertEqual(len(constructor_calls), 2)
        self.assertEqual(
            [call.get("max_retries") for call in constructor_calls],
            [0, 0],
        )

    def test_llm_client_raises_after_three_failed_attempts(self):
        attempts = {"count": 0}

        class FailingCompletions:
            def create(self, **kwargs):
                attempts["count"] += 1
                raise RuntimeError("temporary-network-error")

        class FailingChat:
            completions = FailingCompletions()

        class FailingClient:
            chat = FailingChat()

        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="retry-model",
            api_key="test-key",
        )
        client._client = FailingClient()

        with self.assertRaisesRegex(RuntimeError, "temporary-network-error"):
            client.invoke_json(
                task="business_intent",
                prompt_version="test",
                messages=[{"role": "user", "content": "{}"}],
                required_keys=["ok"],
            )

        self.assertEqual(attempts["count"], 3)

    def test_llm_client_retries_invalid_high_value_narrative_only_at_provider_boundary(self):
        attempts = {"count": 0}

        class ResponseMessage:
            content = '{"answer_text": "Generated evidence-based answer."}'

        class ResponseChoice:
            message = ResponseMessage()

        class Response:
            id = "response-invalid-narrative"
            choices = [ResponseChoice()]
            usage = None

        class InvalidNarrativeCompletions:
            def create(self, **kwargs):
                attempts["count"] += 1
                return Response()

        class InvalidNarrativeChat:
            completions = InvalidNarrativeCompletions()

        class InvalidNarrativeClient:
            chat = InvalidNarrativeChat()

        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="invalid-narrative-model",
            api_key="test-key",
        )
        client._client = InvalidNarrativeClient()

        with self.assertRaisesRegex(
            LLMOutputError,
            "llm_narrative_invalid:answer_text",
        ):
            client.invoke_json(
                task="answer_synthesis",
                prompt_version="test",
                messages=[{"role": "user", "content": "{}"}],
                required_keys=["answer_text"],
            )

        self.assertEqual(attempts["count"], 3)

    def test_llm_client_accepts_business_entities_in_answer_without_retry(self):
        output = {
            "answer_text": (
                "WajeSpecial 渠道与 Lagos 地区是本次增量的主要落点；"
                "Samsung 设备和 Infinix X669 型号呈现不同方向，"
                "Spearman 结果仅作为辅助关联证据。"
            ),
            "display_summary": "已完成渠道、地区和设备定位。",
        }
        client, completions = _provider_client_with_outputs((output,))

        result = client.invoke_json(
            task="answer_synthesis",
            prompt_version="test",
            messages=[{"role": "user", "content": "{}"}],
            required_keys=["answer_text", "display_summary"],
        )

        self.assertEqual(result.output, output)
        self.assertEqual(completions.attempt_count, 1)

    def test_llm_client_retries_empty_required_business_material_at_provider_boundary(self):
        attempts = {"count": 0}

        class RetriedMaterialCompletions:
            def create(self, **kwargs):
                attempts["count"] += 1
                time_window = None if attempts["count"] < 3 else "2026-06-02"

                class ResponseMessage:
                    content = json.dumps({"time_window": time_window})

                class ResponseChoice:
                    message = ResponseMessage()

                class Response:
                    id = "response-retried-business-material"
                    choices = [ResponseChoice()]
                    usage = None

                return Response()

        class RetriedMaterialChat:
            completions = RetriedMaterialCompletions()

        class RetriedMaterialClient:
            chat = RetriedMaterialChat()

        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="retried-material-model",
            api_key="test-key",
        )
        client._client = RetriedMaterialClient()

        result = client.invoke_json(
            task="business_intent",
            prompt_version="test",
            messages=[{"role": "user", "content": "{}"}],
            required_keys=["time_window"],
        )

        self.assertEqual(result.output["time_window"], "2026-06-02")
        self.assertEqual(attempts["count"], 3)
        self.assertEqual(result.audit["attempt_count"], 3)

    def test_llm_client_retries_structurally_invalid_business_material(self):
        invalid_material = {
            "question_family": {"value": "paid_amount_change_explanation"},
            "target_metric": ["paid_amount"],
            "pattern_family": True,
            "scope": {"type": "full_sample"},
            "time_window": {"start": ""},
            "target_claim": ["解释付费金额变化"],
        }

        for field, invalid_value in invalid_material.items():
            with self.subTest(field=field):
                attempts = {"count": 0}

                class InvalidMaterialCompletions:
                    def create(self, **kwargs):
                        attempts["count"] += 1

                        class ResponseMessage:
                            content = json.dumps({field: invalid_value})

                        class ResponseChoice:
                            message = ResponseMessage()

                        class Response:
                            id = f"response-invalid-{field}"
                            choices = [ResponseChoice()]
                            usage = None

                        return Response()

                class InvalidMaterialChat:
                    completions = InvalidMaterialCompletions()

                class InvalidMaterialClient:
                    chat = InvalidMaterialChat()

                client = OpenAICompatibleLLMClient(
                    provider="openai_compatible",
                    model="invalid-material-model",
                    api_key="test-key",
                )
                client._client = InvalidMaterialClient()

                with self.assertRaisesRegex(
                    LLMOutputError,
                    f"invalid_llm_output_material:{field}",
                ) as raised:
                    client.invoke_json(
                        task="business_intent",
                        prompt_version="test",
                        messages=[{"role": "user", "content": "{}"}],
                        required_keys=[field],
                    )

                self.assertEqual(attempts["count"], 3)
                self.assertEqual(raised.exception.audit["attempt_count"], 3)
                self.assertEqual(
                    raised.exception.audit["failure_code"],
                    f"invalid_llm_output_material:{field}",
                )

    def test_llm_client_runs_cross_field_output_validator_inside_retry_loop(self):
        client, completions = _provider_client_with_outputs(
            ({"marker": "invalid"}, {"marker": "invalid"}, {"marker": "valid"})
        )

        def validate_output(output):
            if output["marker"] != "valid":
                raise LLMOutputError("cross_field_output_invalid:marker")

        result = client.invoke_json(
            task="business_intent",
            prompt_version="test",
            messages=[{"role": "user", "content": "{}"}],
            required_keys=["marker"],
            output_validator=validate_output,
        )

        self.assertEqual(result.output, {"marker": "valid"})
        self.assertEqual(completions.attempt_count, 3)
        self.assertEqual(result.audit["attempt_count"], 3)

    def test_business_intent_provider_retries_invalid_pattern_family_with_audit(self):
        for pattern_family in (None, "none", "invented_pattern"):
            with self.subTest(pattern_family=pattern_family):
                output = _provider_business_intent_output(
                    pattern_family=pattern_family,
                )
                client, completions = _provider_client_with_outputs((output, output, output))
                state = {
                    "request": {
                        "question": "检查当前经营表现。",
                        "run_mode": "production",
                    },
                    "llm_client": client,
                    "llm_calls": [],
                    "checkpoint_events": [],
                }

                with self.assertRaises(WorkflowFailure):
                    _understand_business_intent(state)

                self.assertEqual(completions.attempt_count, 3)
                self.assertEqual(state["llm_calls"][-1]["attempt_count"], 3)
                self.assertEqual(state["llm_calls"][-1]["status"], "failed")
                self.assertIn(
                    "pattern_family",
                    state["llm_calls"][-1]["failure_code"],
                )

    def test_business_intent_noncanonical_target_exhaustion_stops_before_clarification(self):
        invalid = _provider_business_intent_output(
            question_family="paid_amount_change_explanation",
            pattern_family="custom_baseline",
            time_window="前一天",
            target_claim="解释目标日付费金额变化及其影响因素。",
            baseline_candidates=[],
        )
        client, completions = _provider_client_with_outputs((invalid,))
        state = {
            "request": {
                "question": "昨天付费金额为什么变化？",
                "run_mode": "live",
                "analysis_context": {},
            },
            "llm_client": client,
            "llm_calls": [],
            "checkpoint_events": [],
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            "business_intent_contract_invalid:time_window",
        ):
            _retrying_node(
                "understand_business_intent",
                _understand_business_intent,
            )(state)

        self.assertEqual(completions.attempt_count, 3)
        self.assertNotIn("intent", state)
        self.assertNotIn("boundary_decision", state)
        self.assertNotIn("clarification_outcome", state)
        self.assertNotIn("query_results", state["request"])
        self.assertEqual(state["request"]["analysis_context"], {})
        self.assertEqual(state["llm_calls"][-1]["status"], "failed")
        self.assertEqual(len(state["llm_calls"][-1]["attempt_failures"]), 3)

    def test_business_intent_keeps_explicit_target_date_separate_from_previous_day_baseline(self):
        invalid = _provider_business_intent_output(
            question_family="paid_amount_change_explanation",
            pattern_family="custom_baseline",
            time_window="previous_day",
            target_claim="解释目标日付费金额变化及其影响因素。",
            baseline_candidates=["same_weekday_last_week", "previous_day"],
        )
        valid = {**invalid, "time_window": "2026-06-01"}
        client, completions = _provider_client_with_outputs((invalid, valid))
        state = {
            "request": {
                "question": (
                    "2026年6月1日付费金额相较上周同日和前一天"
                    "发生了什么变化？"
                ),
                "run_mode": "live",
                "analysis_context": {
                    "as_of": "2026-07-14T12:00:00+01:00",
                },
            },
            "llm_client": client,
            "llm_calls": [],
            "checkpoint_events": [],
        }

        _understand_business_intent(state)

        self.assertEqual(completions.attempt_count, 2)
        self.assertEqual(state["intent"]["target_semantic"], "2026-06-01")
        self.assertEqual(state["intent"]["time_window"], "2026-06-01")
        self.assertEqual(
            state["intent"]["baseline_candidates"],
            ["same_weekday_last_week", "previous_day"],
        )
        self.assertTrue(
            all(
                candidate in workflow_module.CANONICAL_BASELINE_IDS
                for candidate in state["intent"]["baseline_candidates"]
            )
        )
        self.assertEqual(
            state["request"]["analysis_context"]["target_date"],
            "2026-06-01",
        )

    def test_business_intent_provider_requires_pattern_params_mapping(self):
        missing = object()
        for pattern_params in (missing, None, [], "rolling"):
            with self.subTest(pattern_params=pattern_params):
                output = _provider_business_intent_output()
                if pattern_params is missing:
                    output.pop("pattern_params")
                else:
                    output["pattern_params"] = pattern_params
                client, completions = _provider_client_with_outputs((output, output, output))
                state = {
                    "request": {
                        "question": "检查当前经营表现。",
                        "run_mode": "production",
                    },
                    "llm_client": client,
                    "llm_calls": [],
                    "checkpoint_events": [],
                }

                with self.assertRaises(WorkflowFailure):
                    _understand_business_intent(state)

                self.assertEqual(completions.attempt_count, 3)
                self.assertEqual(state["llm_calls"][-1]["attempt_count"], 3)
                self.assertIn(
                    "pattern_params",
                    state["llm_calls"][-1]["failure_code"],
                )

    def test_business_intent_provider_retries_invalid_weekly_target_selector(self):
        for pattern_params in (
            {},
            {"baseline_weekdays": [1, 2, 3, 4, 5]},
            {"target_weekdays": []},
            {"target_weekdays": [""]},
            {"target_weekdays": [{"day": 6}]},
            {"target_weekdays": [[]]},
            {"target_weekday": True},
            {"target_weekday": {"day": 6}},
        ):
            with self.subTest(pattern_params=pattern_params):
                output = _provider_business_intent_output(
                    pattern_family="weekly",
                    pattern_params=pattern_params,
                )
                client, completions = _provider_client_with_outputs((output, output, output))
                state = {
                    "request": {
                        "question": "检查重复的星期形状。",
                        "run_mode": "production",
                    },
                    "llm_client": client,
                    "llm_calls": [],
                    "checkpoint_events": [],
                }

                with self.assertRaises(WorkflowFailure):
                    _understand_business_intent(state)

                self.assertEqual(completions.attempt_count, 3)
                self.assertEqual(state["llm_calls"][-1]["attempt_count"], 3)
                self.assertEqual(
                    state["llm_calls"][-1]["failure_code"],
                    "invalid_llm_output_material:pattern_params:weekly_target_required",
                )

    def test_business_intent_provider_retries_intra_period_without_target_selector(self):
        for pattern_params in (
            {},
            {"target_phase": ""},
            {"target_phase": True},
            {"target_group": None},
            {"target_group": []},
        ):
            with self.subTest(pattern_params=pattern_params):
                output = _provider_business_intent_output(
                    pattern_family="intra_period",
                    pattern_params=pattern_params,
                )
                client, completions = _provider_client_with_outputs((output, output, output))
                state = {
                    "request": {
                        "question": "检查周期内阶段变化。",
                        "run_mode": "production",
                    },
                    "llm_client": client,
                    "llm_calls": [],
                    "checkpoint_events": [],
                }

                with self.assertRaises(WorkflowFailure):
                    _understand_business_intent(state)

                self.assertEqual(completions.attempt_count, 3)
                self.assertEqual(state["llm_calls"][-1]["attempt_count"], 3)
                self.assertEqual(
                    state["llm_calls"][-1]["failure_code"],
                    "invalid_llm_output_material:pattern_params:intra_period_target_required",
                )

    def test_business_intent_provider_accepts_valid_pattern_contracts(self):
        valid_patterns = (
            ("intra_period", {"target_phase": "start"}),
            ("intra_period", {"target_group": "target"}),
            ("rolling", {}),
            ("weekly", {"target_weekday": 6}),
            ("weekly", {"target_weekday": "saturday"}),
            ("weekly", {"target_weekdays": [6, 7]}),
            ("weekly", {"target_weekdays": [6, "sunday"]}),
        )
        for pattern_family, pattern_params in valid_patterns:
            with self.subTest(
                pattern_family=pattern_family,
                pattern_params=pattern_params,
            ):
                output = _provider_business_intent_output(
                    pattern_family=pattern_family,
                    pattern_params=pattern_params,
                )
                client, completions = _provider_client_with_outputs((output,))
                state = {
                    "request": {
                        "question": "检查当前经营表现。",
                        "run_mode": "production",
                    },
                    "llm_client": client,
                    "llm_calls": [],
                    "checkpoint_events": [],
                }

                _understand_business_intent(state)

                self.assertEqual(completions.attempt_count, 1)
                self.assertEqual(state["intent"]["pattern_family"], pattern_family)
                self.assertEqual(state["intent"]["pattern_params"], pattern_params)




    def test_pattern_output_contract_does_not_apply_to_other_tasks(self):
        output = {"pattern_family": "none", "pattern_params": None}
        client, completions = _provider_client_with_outputs((output,))

        result = client.invoke_json(
            task="answer_synthesis",
            prompt_version="test",
            messages=[{"role": "user", "content": "{}"}],
            required_keys=["pattern_family", "pattern_params"],
        )

        self.assertEqual(result.output, output)
        self.assertEqual(completions.attempt_count, 1)

    def test_llm_client_accepts_structured_business_time_window(self):
        class StructuredWindowCompletions:
            def create(self, **kwargs):
                class ResponseMessage:
                    content = json.dumps(
                        {"time_window": {"start": "2026-06-01", "end": "2026-06-02"}}
                    )

                class ResponseChoice:
                    message = ResponseMessage()

                class Response:
                    id = "response-structured-window"
                    choices = [ResponseChoice()]
                    usage = None

                return Response()

        class StructuredWindowChat:
            completions = StructuredWindowCompletions()

        class StructuredWindowClient:
            chat = StructuredWindowChat()

        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="structured-window-model",
            api_key="test-key",
        )
        client._client = StructuredWindowClient()

        result = client.invoke_json(
            task="business_intent",
            prompt_version="test",
            messages=[{"role": "user", "content": "{}"}],
            required_keys=["time_window"],
        )

        self.assertEqual(result.audit["attempt_count"], 1)

    def test_llm_client_does_not_apply_business_material_policy_to_other_tasks(self):
        class OtherTaskCompletions:
            def create(self, **kwargs):
                class ResponseMessage:
                    content = json.dumps({"time_window": {"start": ""}})

                class ResponseChoice:
                    message = ResponseMessage()

                class Response:
                    id = "response-other-task"
                    choices = [ResponseChoice()]
                    usage = None

                return Response()

        class OtherTaskChat:
            completions = OtherTaskCompletions()

        class OtherTaskClient:
            chat = OtherTaskChat()

        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="other-task-model",
            api_key="test-key",
        )
        client._client = OtherTaskClient()

        result = client.invoke_json(
            task="unrelated_task",
            prompt_version="test",
            messages=[{"role": "user", "content": "{}"}],
            required_keys=["time_window"],
        )

        self.assertEqual(result.audit["attempt_count"], 1)

    def test_invoke_llm_records_provider_retry_exhaustion_audit(self):
        failure_audit = {
            "task": "business_intent",
            "provider": "openai_compatible",
            "model": "failed-model",
            "prompt_version": "test",
            "attempt_count": 3,
            "failure_code": "invalid_llm_output_material:time_window",
            "status": "failed",
        }

        class ExhaustedClient:
            def invoke_json(self, **kwargs):
                raise LLMOutputError(
                    "invalid_llm_output_material:time_window",
                    audit=failure_audit,
                )

        state = {
            "llm_client": ExhaustedClient(),
            "llm_calls": [],
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            "invalid_llm_output_material:time_window",
        ):
            workflow_module._invoke_llm(state, "business_intent", {})

        self.assertEqual(state["llm_calls"], [failure_audit])

    def test_llm_client_does_not_cap_json_completion_tokens(self):
        captured = {}

        class ResponseMessage:
            content = '{"ok": true}'

        class ResponseChoice:
            message = ResponseMessage()

        class Response:
            id = "response-token-cap"
            choices = [ResponseChoice()]
            usage = None

        class CapturingCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return Response()

        class CapturingChat:
            completions = CapturingCompletions()

        class CapturingClient:
            chat = CapturingChat()

        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="capturing-model",
            api_key="test-key",
        )
        client._client = CapturingClient()

        result = client.invoke_json(
            task="business_intent",
            prompt_version="test",
            messages=[{"role": "user", "content": "{}"}],
            required_keys=["ok"],
        )

        self.assertEqual(result.output["ok"], True)
        self.assertNotIn("max_tokens", captured)
        self.assertNotIn("max_completion_tokens", captured)
        self.assertEqual(captured["temperature"], 0)

    def test_llm_client_runs_openai_provider_call_in_subprocess(self):
        self.assertEqual(llm_client_module._process_context().get_start_method(), "spawn")
        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="subprocess-model",
            api_key="test-key",
        )
        client._request_worker = scripted_provider_request
        result = client.invoke_json(
            task="business_intent",
            prompt_version="test",
            messages=[{"role": "user", "content": "{}"}],
            required_keys=["ok"],
        )

        self.assertEqual(result.output["ok"], True)
        self.assertEqual(result.audit["response_id"], "subprocess-response")
        self.assertEqual(result.audit["attempt_count"], 1)

    def test_llm_subprocess_drains_large_payload_before_join_with_timeout(self):
        before = {child.pid for child in multiprocessing.active_children()}

        result = llm_client_module._request_openai_json_in_subprocess(
            {"payload_size": 20_000},
            [{"role": "user", "content": "x"}],
            1.0,
            request_worker=large_scripted_provider_request,
        )

        self.assertEqual(len(result), 20_000)
        self.assertEqual(
            {
                child.pid
                for child in multiprocessing.active_children()
                if child.pid not in before
            },
            set(),
        )

    def test_llm_subprocess_drains_large_payload_without_provider_timeout(self):
        before = {child.pid for child in multiprocessing.active_children()}
        try:
            with llm_client_module._wall_clock_timeout(2.0):
                result = llm_client_module._request_openai_json_in_subprocess(
                    {"payload_size": 20_000},
                    [{"role": "user", "content": "x"}],
                    None,
                    request_worker=large_scripted_provider_request,
                )
        finally:
            leaked = [
                child
                for child in multiprocessing.active_children()
                if child.pid not in before
            ]
            for child in leaked:
                if child.is_alive():
                    child.kill()
                child.join()

        self.assertEqual(len(result), 20_000)
        self.assertEqual(leaked, [])

    def test_llm_subprocess_reports_worker_error_and_empty_result_without_leaks(self):
        before = {child.pid for child in multiprocessing.active_children()}
        for worker, message in (
            (spawn_failing_llm_request, "provider-worker-failed"),
            (spawn_exit_without_llm_result, "llm_subprocess_failed:exitcode=7"),
        ):
            with self.subTest(worker=worker.__name__), self.assertRaisesRegex(
                RuntimeError,
                message,
            ):
                llm_client_module._request_openai_json_in_subprocess(
                    {},
                    [{"role": "user", "content": "x"}],
                    1.0,
                    request_worker=worker,
                )
        self.assertEqual(
            {
                child.pid
                for child in multiprocessing.active_children()
                if child.pid not in before
            },
            set(),
        )

    def test_llm_subprocess_timeout_cleans_process_and_ipc(self):
        before = {child.pid for child in multiprocessing.active_children()}
        receiver_threads_before = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name == "waje-llm-provider-receiver" and thread.is_alive()
        }
        with self.assertRaisesRegex(LLMTimeoutError, "llm_request_timeout"):
            llm_client_module._request_openai_json_in_subprocess(
                {},
                [{"role": "user", "content": "x"}],
                0.01,
                request_worker=spawn_safe_stuck_llm_request,
            )
        self.assertEqual(
            {
                child.pid
                for child in multiprocessing.active_children()
                if child.pid not in before
            },
            set(),
        )
        self.assertEqual(
            {
                thread.ident
                for thread in threading.enumerate()
                if thread.name == "waje-llm-provider-receiver" and thread.is_alive()
            },
            receiver_threads_before,
        )

    def test_llm_subprocess_without_timeout_never_kills_eof_alive_child(self):
        receiver_threads_before = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name == "waje-llm-provider-receiver" and thread.is_alive()
        }

        class FakeConnection:
            def __init__(self, *, eof=False):
                self.eof = eof
                self.closed = False

            def poll(self):
                return True

            def recv(self):
                if self.eof:
                    raise EOFError
                return None

            def close(self):
                self.closed = True

        class FakeProcess:
            sentinel = object()

            def __init__(self):
                self.alive = True
                self.exitcode = None
                self.kills = 0
                self.joins = []
                self.closed = False

            def start(self):
                return None

            def join(self, timeout=None):
                self.joins.append(timeout)
                if timeout is None:
                    self.alive = False
                    self.exitcode = 9

            def is_alive(self):
                return self.alive

            def kill(self):
                self.kills += 1
                self.alive = False
                self.exitcode = -9

            def close(self):
                self.closed = True

        output = FakeConnection(eof=True)
        child = FakeConnection()
        process = FakeProcess()

        class FakeContext:
            def Pipe(self, duplex=False):
                return output, child

            def Process(self, **kwargs):
                return process

        with patch.object(
            llm_client_module,
            "_process_context",
            return_value=FakeContext(),
        ), self.assertRaises(RuntimeError) as raised:
            llm_client_module._request_openai_json_in_subprocess(
                {},
                [{"role": "user", "content": "x"}],
                None,
                request_worker=scripted_provider_request,
            )

        self.assertEqual(str(raised.exception), "llm_subprocess_failed:exitcode=9")
        self.assertEqual(process.kills, 0)
        self.assertIn(None, process.joins)
        self.assertTrue(output.closed)
        self.assertTrue(child.closed)
        self.assertTrue(process.closed)
        self.assertEqual(
            {
                thread.ident
                for thread in threading.enumerate()
                if thread.name == "waje-llm-provider-receiver" and thread.is_alive()
            },
            receiver_threads_before,
        )

    def test_llm_subprocess_receiver_cleanup_join_is_bounded(self):
        original_thread = threading.Thread
        receiver_threads = []

        class RecordingThread(original_thread):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.join_timeouts = []
                receiver_threads.append(self)

            def join(self, timeout=None):
                self.join_timeouts.append(timeout)
                return super().join(timeout)

        class FakeConnection:
            def recv(self):
                raise EOFError

            def close(self):
                return None

        class FakeProcess:
            def __init__(self):
                self.alive = True
                self.exitcode = None

            def start(self):
                return None

            def join(self, timeout=None):
                self.alive = False
                self.exitcode = 4

            def is_alive(self):
                return self.alive

            def kill(self):
                raise AssertionError("provider child must not be killed without a timeout")

            def close(self):
                return None

        process = FakeProcess()

        class FakeContext:
            def Pipe(self, duplex=False):
                return FakeConnection(), FakeConnection()

            def Process(self, **kwargs):
                return process

        with patch.object(
            llm_client_module,
            "_process_context",
            return_value=FakeContext(),
        ), patch.object(
            llm_client_module.threading,
            "Thread",
            RecordingThread,
        ), self.assertRaisesRegex(
            RuntimeError,
            "llm_subprocess_failed:exitcode=4",
        ):
            llm_client_module._request_openai_json_in_subprocess(
                {},
                [{"role": "user", "content": "x"}],
                None,
                request_worker=scripted_provider_request,
            )

        self.assertEqual(len(receiver_threads), 1)
        cleanup_join_timeout = receiver_threads[0].join_timeouts[-1]
        self.assertIsNotNone(cleanup_join_timeout)
        self.assertGreater(cleanup_join_timeout, 0)
        self.assertFalse(receiver_threads[0].is_alive())

    def test_llm_subprocess_closes_process_handle_before_receiver_cleanup_failure(self):
        class StuckReceiver:
            def __init__(self, *args, **kwargs):
                self.join_timeouts = []

            def start(self):
                return None

            def join(self, timeout=None):
                self.join_timeouts.append(timeout)

            def is_alive(self):
                return True

        class FakeConnection:
            def __init__(self):
                self.closed = False

            def recv(self):
                raise AssertionError("stuck receiver does not execute its target")

            def close(self):
                self.closed = True

        class FakeProcess:
            def __init__(self):
                self.alive = True
                self.exitcode = None
                self.closed = False

            def start(self):
                return None

            def join(self, timeout=None):
                return None

            def is_alive(self):
                return self.alive

            def kill(self):
                self.alive = False
                self.exitcode = -9

            def close(self):
                self.closed = True

        output = FakeConnection()
        child = FakeConnection()
        process = FakeProcess()

        class FakeContext:
            def Pipe(self, duplex=False):
                return output, child

            def Process(self, **kwargs):
                return process

        with patch.object(
            llm_client_module,
            "_process_context",
            return_value=FakeContext(),
        ), patch.object(
            llm_client_module.threading,
            "Thread",
            StuckReceiver,
        ), self.assertRaisesRegex(
            RuntimeError,
            "llm_receiver_cleanup_timeout",
        ):
            llm_client_module._request_openai_json_in_subprocess(
                {},
                [{"role": "user", "content": "x"}],
                0.01,
                request_worker=scripted_provider_request,
            )

        self.assertTrue(output.closed)
        self.assertTrue(child.closed)
        self.assertTrue(process.closed)

    def test_llm_subprocess_rejects_result_received_after_total_deadline(self):
        class FakeConnection:
            def __init__(self, *, slow=False):
                self.slow = slow

            def poll(self):
                return True

            def recv(self):
                if self.slow:
                    time.sleep(0.05)
                return {"ok": True, "result": {"ok": True}}

            def close(self):
                return None

        class FakeProcess:
            sentinel = object()

            def __init__(self):
                self.alive = True
                self.exitcode = None
                self.kills = 0

            def start(self):
                return None

            def join(self, timeout=None):
                self.alive = False
                self.exitcode = 0

            def is_alive(self):
                return self.alive

            def kill(self):
                self.kills += 1
                self.alive = False
                self.exitcode = -9

            def close(self):
                return None

        output = FakeConnection(slow=True)
        child = FakeConnection()
        process = FakeProcess()

        class FakeContext:
            def Pipe(self, duplex=False):
                return output, child

            def Process(self, **kwargs):
                return process

        with patch.object(
            llm_client_module,
            "_process_context",
            return_value=FakeContext(),
        ), self.assertRaisesRegex(LLMTimeoutError, "llm_request_timeout"):
            llm_client_module._request_openai_json_in_subprocess(
                {},
                [{"role": "user", "content": "x"}],
                0.01,
                request_worker=scripted_provider_request,
            )

    def test_llm_client_does_not_fall_back_to_fork_context(self):
        class ForkContext:
            def get_start_method(self):
                return "fork"

        def fake_get_context(method=None):
            if method == "spawn":
                raise ValueError("spawn unavailable")
            return ForkContext()

        with patch.object(llm_client_module.multiprocessing, "get_context", fake_get_context):
            with self.assertRaisesRegex(LLMConfigurationError, "spawn_start_method_unavailable"):
                llm_client_module._process_context()

    def test_llm_client_kills_stuck_subprocess_without_workflow_retry(self):
        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="subprocess-model",
            api_key="test-key",
            timeout_seconds=0.01,
            max_attempts=1,
        )
        client._request_worker = spawn_safe_stuck_llm_request
        with self.assertRaisesRegex(LLMTimeoutError, "llm_request_timeout"):
            client.invoke_json(
                task="business_intent",
                prompt_version="test",
                messages=[{"role": "user", "content": "{}"}],
                required_keys=["ok"],
            )

    def test_llm_client_retries_after_killing_timed_out_subprocess(self):
        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="subprocess-model",
            api_key="test-key",
            timeout_seconds=1.5,
            max_attempts=2,
        )
        worker = SpawnTimeoutThenSuccessWorker()
        client._request_worker = worker

        result = client.invoke_json(
            task="business_intent",
            prompt_version="test",
            messages=[{"role": "user", "content": "{}"}],
            required_keys=["ok"],
        )

        self.assertEqual(result.output["ok"], True)
        self.assertEqual(result.audit["response_id"], "subprocess-retry-success")
        self.assertEqual(result.audit["attempt_count"], 2)

    def test_llm_narrative_normalization_keeps_machine_tokens_without_templates(self):
        output = _localize_narrative_fields(
            {
                "status_message": "已完成当前业务判断。",
                "target_metric": "paid_amount",
                "recommended_assumption": "产品默认的材料性和稳定性规则，不使用p值。",
                "route_summary": "使用compare_period_phases和metric_timeseries分析paid_amount，不说显著性。",
                "accepted_assumptions": ["scope为full_sample，min_periods=20。"],
                "confirmed_intent": {
                    "business_summary": "scope为full_sample，材料性规则。",
                    "machine_intent": {"scope": "full_sample"},
                },
                "decision_summary": (
                    "scope为full_sample，对账单强度按默认处理，"
                    "模式参数min_periods=20，"
                    "使用产品默认的重要性和稳定性规则"
                    "（product default materiality and stability rules），"
                    "超过默认重要性阈值（例如5%）。"
                ),
            }
        )

        self.assertEqual(output["status_message"], "已完成当前业务判断。")
        self.assertEqual(output["target_metric"], "paid_amount")
        self.assertEqual(
            output["recommended_assumption"],
            "产品默认的重要性和稳定性规则，不使用重要性规则。",
        )
        self.assertEqual(
            output["route_summary"],
            "使用周期内阶段对比和指标时间序列分析付费金额，不说重要性。",
        )
        self.assertEqual(output["accepted_assumptions"], ["范围为全样本，至少20个可比周期。"])
        self.assertEqual(
            output["confirmed_intent"]["business_summary"],
            "范围为全样本，重要性规则。",
        )
        self.assertEqual(output["confirmed_intent"]["machine_intent"]["scope"], "full_sample")
        self.assertEqual(
            output["decision_summary"],
            (
                "范围为全样本，结论强度按默认处理，"
                "窗口规则至少20个可比周期，使用产品默认的重要性和稳定性规则，"
                "超过默认重要性阈值。"
            ),
        )
    def test_llm_narrative_rejects_scalar_accepted_assumptions(self):
        with self.assertRaisesRegex(
            LLMOutputError,
            "llm_narrative_invalid:accepted_assumptions",
        ):
            _localize_narrative_fields(
                {"accepted_assumptions": "沿用当前业务口径继续。"}
            )

    def test_llm_narrative_invalid_high_value_text_fails_without_local_template(self):
        for key, value in (
            ("answer_text", "Generated evidence-based answer."),
            ("summary_text", {"unexpected": "shape"}),
            ("text", "Correlation observed."),
            ("business_summary", None),
        ):
            with self.subTest(key=key):
                with self.assertRaisesRegex(
                    LLMOutputError,
                    f"llm_narrative_invalid:{key}",
                ):
                    _localize_narrative_fields({key: value})

    def test_llm_narrative_keeps_accepted_assumptions_flat(self):
        output = _localize_narrative_fields(
            {
                "accepted_assumptions": [
                    "假设基线分为月中和月末两个阶段。",
                    "使用产品默认重要性规则。",
                ]
            }
        )

        self.assertEqual(
            output["accepted_assumptions"],
            ["假设基线分为月中和月末两个阶段。", "使用产品默认重要性规则。"],
        )

    def test_llm_narrative_allows_business_entity_tokens(self):
        output = _localize_narrative_fields(
            {
                "description": "所有非FooBar2026渠道的汇总",
                "business_summary": "对比Alpha渠道和Beta渠道的付费金额表现。",
            }
        )

        self.assertEqual(output["description"], "所有非FooBar2026渠道的汇总")
        self.assertEqual(
            output["business_summary"],
            "对比Alpha渠道和Beta渠道的付费金额表现。",
        )

    def test_llm_narrative_allows_uppercase_business_acronyms(self):
        output = _localize_narrative_fields(
            {
                "business_summary": "ROI、DAU 与 ARPPU 同步改善，收入按 NGN 计价。",
                "owner": "WAJE 业务分析团队",
                "recommendation_reason": "该方案有利于复核 ROI 变化。",
            }
        )

        self.assertIn("ROI", output["business_summary"])
        self.assertIn("NGN", output["business_summary"])
        self.assertEqual(output["owner"], "WAJE 业务分析团队")

    def test_llm_narrative_allows_mixed_language_business_entities_and_methods(self):
        output = _localize_narrative_fields(
            {
                "answer_text": (
                    "WajeSpecial 渠道贡献较集中，Samsung 设备与 Lagos 地区同步增长；"
                    "Spearman 相关结果只能作为辅助证据。"
                ),
                "display_summary": "Infinix X669 是需要继续核验的设备型号。",
            }
        )

        self.assertIn("WajeSpecial", output["answer_text"])
        self.assertIn("Spearman", output["answer_text"])
        self.assertIn("Infinix X669", output["display_summary"])

    def test_llm_narrative_rejects_machine_identifiers_and_raw_sql(self):
        for key, value in (
            ("answer_text", "内部 evidence_ref 不得出现在业务答案。"),
            ("summary_text", "当前 candidate_hypothesis 仍待确认。"),
            ("explanation", "查询为 SELECT amount FROM paid_order_detail。"),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(
                LLMOutputError,
                f"llm_narrative_invalid:{key}",
            ):
                _localize_narrative_fields({key: value})

    def test_llm_narrative_removes_invented_default_stability_percent(self):
        output = _localize_narrative_fields(
            {
                "recommended_assumption": (
                    "使用产品默认的稳定性和重要性规则：要求目标渠道的月度日均付费金额"
                    "在至少80%的月份中高于基准渠道，且平均高出比例超过产品默认的重要性阈值。"
                )
            }
        )

        self.assertNotIn("80%", output["recommended_assumption"])
        self.assertIn("足够多的月份", output["recommended_assumption"])

    def test_llm_narrative_removes_invented_period_stability_percent(self):
        output = _localize_narrative_fields(
            {
                "recommended_assumption": (
                    "使用产品默认规则：要求目标版本在至少75%的周期中高于基准版本，"
                    "并超过产品默认的重要性阈值。"
                )
            }
        )

        self.assertNotIn("75%", output["recommended_assumption"])
        self.assertIn("足够多的周期", output["recommended_assumption"])

    def test_llm_narrative_normalizes_next_action_technical_terms(self):
        output = _localize_narrative_fields(
            {
                "decision_summary": (
                    "Pattern证据不足，pattern_status: high，"
                    "中等置信度，medium，allow_question_interrupt=false，"
                    "材料阈值和物质性下限，模式稳定可靠，"
                    "模式确认性高，目标vs基线成立，幅度显著，选择synthesize_answer而非degrade。"
                ),
                "next_action": "synthesize_answer",
            }
        )

        self.assertNotIn("Pattern", output["decision_summary"])
        self.assertNotIn("pattern_status", output["decision_summary"])
        self.assertNotIn("synthesize_answer", output["decision_summary"])
        self.assertNotIn("degrade", output["decision_summary"])
        self.assertNotIn("置信度", output["decision_summary"])
        self.assertNotIn("medium", output["decision_summary"])
        self.assertNotIn("allow_question_interrupt", output["decision_summary"])
        self.assertNotIn("vs", output["decision_summary"])
        self.assertNotIn("材料阈值", output["decision_summary"])
        self.assertNotIn("物质性", output["decision_summary"])
        self.assertNotIn("稳定可靠", output["decision_summary"])
        self.assertNotIn("模式确认性高", output["decision_summary"])
        self.assertNotIn("显著", output["decision_summary"])
        self.assertIn("本次对比证据较强", output["decision_summary"])
        self.assertIn("目标相比基线", output["decision_summary"])
        self.assertIn("重要性阈值", output["decision_summary"])
        self.assertEqual(output["next_action"], "synthesize_answer")

    def test_llm_narrative_preserves_route_summary_after_token_cleanup(self):
        output = _localize_narrative_fields(
            {
                "route_summary": (
                    "首先使用data_quality_profile检查2026年Q1和Q2日均付费金额的数据覆盖和质量，"
                    "然后使用compare_periods直接比较两个季度的日均付费金额，"
                    "最后使用answer_verify验证最终声明。这三者均支持custom_baseline_comparison问题族，"
                    "且成本较低，适合在research模式下优先保证答案质量。"
                )
            }
        )

        self.assertNotEqual(output["route_summary"], "已完成分析路线设计。")
        self.assertNotIn("data_quality_profile", output["route_summary"])
        self.assertNotIn("custom_baseline_comparison", output["route_summary"])
        self.assertNotIn("research", output["route_summary"])
        self.assertNotIn("检查检查", output["route_summary"])
        self.assertIn("数据质量检查能力检查", output["route_summary"])
        self.assertIn("周期对比", output["route_summary"])
        self.assertIn("答案校验", output["route_summary"])

    def test_capability_path_labels_include_public_route_nodes(self):
        self.assertEqual(
            _capability_path_labels(
                ("data_quality_profile", "compare_periods", "answer_verify")
            ),
            "数据质量检查、周期对比、答案校验",
        )

    def test_route_output_alignment_removes_replaced_candidate_paths(self):
        output = _align_route_output_to_requested(
            {
                "requested_nodes": ["data_quality_profile", "compare_periods", "answer_verify"],
                "route_summary": "先用metric_timeseries，再用rolling_window_compare。",
                "expected_evidence": ["metric_timeseries evidence", "rolling_window_compare evidence"],
                "decision_summary": "选择rolling_window_compare。",
            },
            ("data_quality_profile", "compare_periods", "answer_verify"),
        )
        text = json.dumps(output, ensure_ascii=False)

        self.assertNotIn("metric_timeseries", text)
        self.assertNotIn("rolling_window_compare", text)
        self.assertIn("周期对比", text)

    def test_evidence_interpretation_normalizes_custom_baseline_direction(self):
        output = {
            "interpretation": "当前证据基于一次性基线比较（Q1 相比 Q2），中位数提升15%。",
            "decision_summary": "Q1相比Q2的结果成立。",
            "evidence_boundary": "不推断长期趋势。",
        }
        state = {
            "request": {},
            "intent": {
                "pattern_family": "custom_baseline",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
            }
        }

        normalized = _normalize_evidence_interpretation_output(output, state)

        text = " ".join(normalized.values())
        self.assertNotIn("Q1 相比 Q2", text)
        self.assertNotIn("Q1相比Q2", text)
        self.assertNotIn("中位数", text)
        self.assertIn("Q2 相比 Q1", text)
        self.assertIn("对比提升", text)

    def test_evidence_interpretation_businessizes_negative_pattern_result(self):
        output = {
            "interpretation": (
                "目标索赔缺乏统计支持，中位提升为-7.1%，"
                "全部月份均未超过重要性阈值（0.03）。"
            ),
            "decision_summary": "目标索赔缺乏统计支持，方向比指向反方向。",
            "evidence_boundary": "共90个数据点，结果受低于重要性阈值的限制。",
        }
        state = {
            "intent": {"pattern_family": "intra_period"},
            "evidence_brief": {"limitations": ["weak_direction", "below_materiality_floor"]},
            "evidence": [
                {
                    "capability_id": "compare_period_phases",
                    "typed_payload": {
                        "pattern_family": "intra_period",
                        "median_uplift": -0.071,
                        "direction_ratio": 0.1,
                        "comparable_periods": 30,
                    },
                }
            ],
        }

        normalized = _normalize_evidence_interpretation_output(output, state)
        text = " ".join(normalized.values())

        self.assertNotIn("目标索赔", text)
        self.assertNotIn("中位提升为-", text)
        self.assertNotIn("全部月份均未超过", text)
        self.assertNotIn("90个数据点", text)
        self.assertIn("目标声明", text)
        self.assertIn("中位变化为下降 7.1%", text)
        self.assertIn("正向月份也未达到当前重要性阈值", text)
        self.assertIn("90个阶段聚合点", text)

    def test_default_claim_preserves_daily_average_business_metric(self):
        state = {
            "intent": {
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2026-01-01..2026-06-30",
                "target_claim": "2026年Q2的日均付费金额高于2026年Q1的日均付费金额",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
            },
            "evidence_brief": {"limitations": []},
            "evidence": [
                {
                    "evidence_ref": "compare_periods:run-1",
                    "capability_id": "compare_periods",
                    "strength": "high",
                    "wording_limit": "supported",
                    "numeric_facts": {
                        "target_value": 115.04,
                        "baseline_value": 100.0,
                        "absolute_change": 15.04,
                        "relative_change": 0.1504,
                    },
                    "typed_payload": {
                        "pattern_family": "custom_baseline",
                        "target_value": 115.04,
                        "baseline_value": 100.0,
                        "absolute_change": 15.04,
                        "relative_change": 0.1504,
                        "comparison_direction": "positive",
                        "target": {"label": "Q2"},
                        "baseline": {"label": "Q1"},
                    },
                }
            ],
        }

        claim = _default_claim_from_evidence(state)

        self.assertIn("Q2的日均付费金额", claim["text"])
        self.assertIn("较Q1", claim["text"])
        self.assertIn("上涨 15.04%", claim["text"])
        self.assertNotIn("中位数", claim["text"])
        self.assertNotIn("方向命中率", claim["text"])
        self.assertNotIn("可比周期", claim["text"])
        self.assertEqual(claim["numbers"]["target_value"], 115.04)
        self.assertEqual(claim["numbers"]["baseline_value"], 100.0)

    def test_default_claim_explains_joint_attribution_business_result(self):
        state = {
            "intent": {
                "question_family": "segment_or_factor_attribution",
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "all_users",
                "time_window": "2026-01-01..2026-06-30",
                "target_claim": "渠道和月内阶段组合是否解释Q2相比Q1的主要变化",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
            },
            "request": {"question": "渠道和月内阶段组合是否解释Q2相比Q1的主要变化？"},
            "evidence_brief": {
                "limitations": [
                    "skipped_incomplete_joint_combinations",
                    "sparse_cell",
                ]
            },
            "evidence": [
                {
                    "evidence_ref": "segment_contribution:inline",
                    "capability_id": "segment_contribution",
                    "strength": "low",
                    "wording_limit": "insufficient",
                    "limitations": ["no_comparable_segments"],
                    "typed_payload": {},
                },
                {
                    "evidence_ref": "joint_attribution:inline",
                    "capability_id": "joint_attribution",
                    "strength": "medium",
                    "wording_limit": "candidate",
                    "limitations": [
                        "skipped_incomplete_joint_combinations",
                        "sparse_cell",
                    ],
                    "typed_payload": {
                        "top_3_absolute_delta_share": 0.425,
                        "leading_absolute_delta_share": 0.168,
                        "total_delta": 3984843236.0,
                        "absolute_total_delta": 3984843236.0,
                        "combination_count": 10,
                        "skipped_sparse_rows": 10,
                        "top_combinations": [
                            {
                                "dimension_values": ["WajeSpecial", "start"],
                                "delta": 668574193.0,
                                "absolute_delta_share": 0.168,
                            },
                            {
                                "dimension_values": ["WajeSpecial", "end"],
                                "delta": 603941864.0,
                                "absolute_delta_share": 0.152,
                            },
                            {
                                "dimension_values": ["WajeSpecial", "mid"],
                                "delta": 422894415.0,
                                "absolute_delta_share": 0.105,
                            },
                        ],
                    },
                }
            ],
        }

        claim = _default_claim_from_evidence(state)
        state["draft_claims"] = [claim]

        self.assertIn("WajeSpecial × 月初", claim["text"])
        self.assertIn("合计占绝对变化 42.5%", claim["text"])
        self.assertIn("观察性归因", claim["text"])
        self.assertNotIn("有边界的业务判断", claim["text"])
        self.assertEqual(claim["evidence_refs"], ["joint_attribution:inline"])
        self.assertEqual(claim["numbers"]["top_3_absolute_delta_share"], 0.425)
        self.assertTrue(
            _final_summary_needs_display_repair(
                "我对问题的理解是：已理解。\n"
                "分析脉络：已分析。\n"
                "关键发现：当前证据可以支持一个有边界的业务判断。\n"
                "最终结论：当前证据支持一个有边界的业务判断。\n"
                "需要注意：保留边界。",
                state,
            )
        )

    def test_authority_claim_prefers_established_joint_ref_and_weakens_causal_wording(self):
        state = {
            "intent": {
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "all_users",
                "time_window": "2026-01-01..2026-06-30",
            },
            "evidence": [
                {
                    "evidence_ref": "data_quality_profile:run-1",
                    "capability_id": "data_quality_profile",
                    "strength": "low",
                    "wording_limit": "degraded",
                    "typed_payload": {"scope": "all_users", "time_window": "2026-01-01..2026-06-30"},
                },
                {
                    "evidence_ref": "joint_attribution:inline",
                    "capability_id": "joint_attribution",
                    "strength": "medium",
                    "wording_limit": "candidate",
                    "typed_payload": {
                        "scope": "all_users",
                        "time_window": "2026-01-01..2026-06-30",
                        "top_3_absolute_delta_share": 0.425,
                    },
                },
            ],
        }

        claims = _normalize_authority_claim_candidates(
            [
                {
                    "text": "组合贡献可以作为候选解释，但不能直接写成因果结论，也不能直接定因果。",
                    "evidence_refs": [
                        "data_quality_profile:run-1",
                        "joint_attribution:inline",
                    ],
                    "numbers": {"top_3_absolute_delta_share": 0.425},
                }
            ],
            state,
        )

        self.assertEqual(claims[0]["evidence_refs"][0], "joint_attribution:inline")
        self.assertNotIn("因果结论", claims[0]["text"])
        self.assertNotIn("定因果", claims[0]["text"])
        self.assertIn("原因定论", claims[0]["text"])

    def test_production_semantic_failure_routes_to_verifier_after_repair_budget(self):
        state = {
            "request": {"run_mode": "production"},
            "semantic_audit": {
                "audit_status": "needs_revision",
                "issues": ["weak_business_interpretation"],
            },
            "semantic_repair_attempts": 1,
            "answer_text": "昨日活跃用户下降。",
            "draft_claims": [],
        }

        self.assertEqual(_route_after_semantic_audit(state), "verify")
        self.assertEqual(state["answer_text"], "昨日活跃用户下降。")
        self.assertEqual(state["draft_claims"], [])

    def test_final_llm_audit_warning_does_not_rewrite_answer(self):
        state = {
            "request": {"run_mode": "production", "question": "昨日活跃用户如何变化？"},
            "answer_text": "昨日活跃用户下降。",
            "final_business_summary": "昨日活跃用户下降。",
            "draft_claims": [],
            "verifier": {"errors": []},
            "evidence": [],
        }
        warning = {
            "display_status": "ready_with_warnings",
            "hard_blockers": [],
            "repairable_warnings": ["weak_business_interpretation"],
            "risk_flags": [],
            "retry_instruction": "补充业务解释。",
            "business_audit_summary": "答案可展示。",
            "blocks_display": False,
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow._final_answer_audit",
            return_value=warning,
        ), patch("bi_agent.runtime.langgraph_workflow._invoke_llm") as summary_rewrite:
            _answer_quality_gate(state)

        summary_rewrite.assert_not_called()
        self.assertEqual(state["final_business_summary"], "昨日活跃用户下降。")
        self.assertFalse(state["quality_gate"]["blocks_display"])

    def test_final_llm_audit_failure_is_a_nonblocking_risk(self):
        state = {
            "request": {"run_mode": "production", "question": "昨日活跃用户如何变化？"},
            "answer_text": "昨日活跃用户下降。",
            "final_business_summary": "昨日活跃用户下降。",
            "draft_claims": [],
            "verifier": {"errors": []},
            "evidence": [],
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow._final_answer_audit",
            side_effect=WorkflowFailure("llm_response_timeout", failure_type="llm"),
        ):
            _answer_quality_gate(state)

        self.assertEqual(state["final_business_summary"], "昨日活跃用户下降。")
        self.assertFalse(state["quality_gate"]["blocks_display"])
        self.assertIn("final_answer_audit_unavailable", state["quality_gate"]["risk_flags"])
        self.assertEqual(
            state["quality_gate"]["business_audit_summary"],
            "最终表达审阅本轮暂不可用，不影响已验证结论的展示。",
        )
        self.assertNotIn(
            "llm_response_timeout",
            state["quality_gate"]["business_audit_summary"],
        )

    def test_ambiguous_authority_refs_are_rejected_without_guessing_a_claim_contract(self):
        evidence = []
        for index, scope in enumerate(("market", "channel"), start=1):
            evidence.append(
                {
                    "evidence_ref": f"evidence:{index}",
                    "binding_manifest_ref": f"binding:{index}",
                    "input_status": "ready",
                    "claim_type": "comparative_change",
                    "supported_claim_types": ("comparative_change",),
                    "strength": "directional",
                    "scope": scope,
                    "time_window": "昨天与前天",
                    "numeric_facts": {"target_value": index},
                    "typed_payload": {"target_value": index},
                }
            )
        state = {
            "intent": {
                "pattern_family": "custom_baseline",
                "target_metric": "active_users",
                "scope": "用户口语范围",
                "time_window": "用户口语窗口",
            },
            "evidence": evidence,
        }

        claims = _normalize_authority_claim_candidates(
            [
                {
                    "claim_text": "存在变化。",
                    "evidence_refs": ["evidence:1", "evidence:2"],
                    "scope": "用户表达",
                    "time_window": "用户窗口",
                    "unknown_value": 999,
                }
            ],
            state,
        )

        self.assertEqual(claims, [])

    def test_final_summary_display_repair_preserves_complete_numeric_claim_slots(self):
        claim = {
            "text": "Q2 相比 Q1 在 2026-01-01..2026-06-30 观察到：日均付费金额提升 15.0%，方向命中率 100.0%，1 个可比周期。",
            "numbers": {
                "median_uplift": 0.1504,
                "direction_ratio": 1.0,
                "comparable_periods": 1,
            },
            "evidence_refs": ["compare_periods:run-1"],
            "scope": "full_sample",
            "time_window": "2026-01-01..2026-06-30",
        }
        state = {
            "intent": {
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2026-01-01..2026-06-30",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
            },
            "draft_claims": [claim],
            "evidence": [
                {
                    "evidence_ref": "compare_periods:run-1",
                    "capability_id": "compare_periods",
                    "typed_payload": {
                        "pattern_family": "custom_baseline",
                        "median_uplift": 0.1504,
                        "direction_ratio": 1.0,
                        "comparable_periods": 1,
                    },
                }
            ],
        }
        summary = (
            "我对问题的理解是：你想看Q2相比Q1的日均付费金额。\n"
            "分析脉络：已完成期间对比。\n"
            "关键发现：日均付费金额提升 15.0%，方向命中率 100.0%，1 个可比周期。\n"
            "最终结论：结论经过语义审计和硬验证，证据充分。\n"
            "需要注意：不能外推为长期稳定规律。"
        )

        self.assertFalse(_final_summary_needs_display_repair(summary, state))

    def test_final_summary_display_repair_accepts_compact_percent_text(self):
        claim = {
            "text": "当前证据不支持月初高于月中和月末。",
            "numbers": {
                "median_uplift": -0.0712977193565374,
                "direction_ratio": 0.1,
                "comparable_periods": 30,
            },
            "evidence_refs": ["compare_period_phases:run-1"],
            "scope": "full_sample",
            "time_window": "2024-01-01..2026-06-30",
        }
        state = {
            "intent": {
                "pattern_family": "intra_period",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01-01..2026-06-30",
            },
            "draft_claims": [claim],
            "evidence": [
                {
                    "evidence_ref": "compare_period_phases:run-1",
                    "capability_id": "compare_period_phases",
                    "typed_payload": {
                        "pattern_family": "intra_period",
                        "median_uplift": -0.0712977193565374,
                        "direction_ratio": 0.1,
                        "comparable_periods": 30,
                    },
                }
            ],
        }
        summary = (
            "我对问题的理解是：判断月初是否高于月中和月末。\n"
            "分析脉络：按月内阶段对比。\n"
            "关键发现：30个可比月份中，仅10%（3个月）支持该假设，中位变化为-7.1%。\n"
            "最终结论：当前统计证据不支持该假设。\n"
            "需要注意：结论不能外推为机制解释。"
        )

        self.assertFalse(_final_summary_needs_display_repair(summary, state))

    def test_final_summary_display_repair_preserves_complete_degraded_summary(self):
        state = {
            "intent": {
                "pattern_family": "intra_period",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2026-01-01..2026-06-30",
            },
            "draft_claims": [],
            "evidence_brief": {
                "limitations": ["below_materiality_floor", "weak_direction"],
            },
            "final_explanation": {"status": "degraded"},
        }
        summary = (
            "我对问题的理解是：你想判断月初是否更高。\n"
            "分析脉络：由于数据量级较小，分析能力受到限制。\n"
            "关键发现：变化幅度低于重要性阈值。\n"
            "最终结论：当前证据不足以支持主张。\n"
            "需要注意：建议延长至全年或多年，或补充合同条款后再看。"
        )

        self.assertFalse(_final_summary_needs_display_repair(summary, state))

    def test_final_summary_display_repair_requires_complete_pattern_evidence_slots(self):
        state = {
            "intent": {
                "pattern_family": "intra_period",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01-01..2026-06-30",
            },
            "draft_claims": [],
            "evidence": [
                {
                    "capability_id": "compare_period_phases",
                    "typed_payload": {
                        "pattern_family": "intra_period",
                        "median_uplift": -0.0713,
                        "direction_ratio": 0.2667,
                        "direction_consistency_ratio": 0.2667,
                        "materiality_hit_ratio": 0.1,
                        "comparable_periods": 30,
                        "min_periods": 30,
                        "materiality_floor": 0.03,
                    },
                    "limitations": ["below_materiality_floor", "weak_direction"],
                    "strength": "low",
                    "wording_limit": "insufficient",
                }
            ],
            "evidence_brief": {
                "limitations": ["below_materiality_floor", "weak_direction"],
            },
            "final_explanation": {"status": "degraded"},
        }
        summary = (
            "我对问题的理解是：你想判断月初是否高于月中和月末。\n"
            "分析脉络：我按月份对比月初和其他阶段。\n"
            "关键发现：中位下降 7.1%，方向一致比例 26.7%。\n"
            "最终结论：当前证据不支持该假设。\n"
            "需要注意：仍需保留当前证据边界。"
        )

        self.assertTrue(_final_summary_needs_display_repair(summary, state))
        complete_summary = summary.replace(
            "方向一致比例 26.7%。",
            "方向一致比例 26.7%，30 个可比周期。",
        )
        self.assertFalse(_final_summary_needs_display_repair(complete_summary, state))

    def test_degraded_final_summary_without_primary_numbers_needs_repair(self):
        state = {
            "intent": {
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01-01..2026-06-30",
                "baseline": {"label": "其他渠道合计"},
                "target": {"label": "WajeSpecial渠道"},
            },
            "draft_claims": [],
            "evidence": [
                {
                    "capability_id": "rolling_window_compare",
                    "typed_payload": {
                        "pattern_family": "custom_baseline",
                        "median_uplift": 1.5728228306622776,
                        "direction_ratio": 0.9310344827586207,
                        "comparable_periods": 29,
                    },
                    "limitations": ["insufficient_comparable_periods"],
                }
            ],
            "evidence_brief": {"limitations": ["insufficient_comparable_periods"]},
            "final_explanation": {"status": "degraded"},
        }

        summary = (
            "我对问题的理解是：你想看 WajeSpecial渠道 相比 其他渠道合计 的日均付费金额是否有明显变化。\n"
            "分析脉络：我先确认问题边界、数据口径和可执行分析路径。\n"
            "关键发现：当前证据不足，不能发布主业务结论。\n"
            "最终结论：当前证据不足以发布这个主结论。\n"
            "需要注意：补充更多可比周期。"
        )

        self.assertTrue(_final_summary_needs_display_repair(summary, state))

    def test_final_summary_repair_requires_driver_claim_numbers(self):
        state = {
            "intent": {
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "all_users",
                "time_window": "2026-01-01..2026-06-30",
                "baseline": {"label": "2026年Q1"},
                "target": {"label": "2026年Q2"},
            },
            "request": {
                "question": "2026年Q2相比Q1付费金额提升，主要是付费用户数增加还是单付费用户金额提升带来的？"
            },
            "draft_claims": [
                {
                    "text": "2026年Q2相比2026年Q1，付费金额提升约16.3%",
                    "numbers": {"median_uplift": 0.1632579798864855},
                    "evidence_refs": ["compare_periods:run-1"],
                    "scope": "all_users",
                    "time_window": "2026-01-01..2026-06-30",
                },
                {
                    "text": "单付费用户金额是主要贡献项，贡献65.4%；付费用户数贡献34.6%。",
                    "numbers": {
                        "unit_value_share": 0.6537576498494277,
                        "volume_share": 0.3462423501505722,
                    },
                    "evidence_refs": ["driver_decomposition:inline"],
                    "scope": "all_users",
                    "time_window": "2026-01-01..2026-06-30",
                },
            ],
            "evidence": [
                {
                    "evidence_ref": "driver_decomposition:inline",
                    "capability_id": "driver_decomposition",
                    "typed_payload": {
                        "decompositions": [
                            {
                                "primary_driver": "unit_value",
                                "volume_key": "paid_users",
                                "unit_value_share": 0.6537576498494277,
                                "volume_share": 0.3462423501505722,
                                "amount_delta_ratio": 0.1632579798864855,
                            }
                        ]
                    },
                    "strength": "high",
                    "wording_limit": "quantified",
                }
            ],
        }
        incomplete = (
            "我对问题的理解是：你想看Q2相比Q1。\n"
            "分析脉络：我做了周期对比和驱动拆解。\n"
            "关键发现：付费金额提升 16.3%。\n"
            "最终结论：付费金额提升约16.3%。\n"
            "需要注意：不能外推为长期规律。"
        )

        self.assertTrue(_final_summary_needs_display_repair(incomplete, state))

    def test_next_action_ask_degrades_when_evidence_has_terminal_business_boundary(self):
        state = {
            "request": {"allow_question_interrupt": True},
            "checkpoint_events": [{"node": "decide_next_action"}],
            "next_action": {
                "next_action": "ask_question",
                "decision_summary": "建议用户调整稳定性规则。",
            },
            "intent": {
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01-01..2026-06-30",
            },
            "evidence_brief": {"limitations": ["insufficient_comparable_periods"]},
            "evidence": [
                {
                    "evidence_ref": "compare_periods:terminal-boundary",
                    "capability_id": "compare_periods",
                    "typed_payload": {
                        "pattern_family": "custom_baseline",
                        "comparable_periods": 29,
                    },
                    "limitations": ["insufficient_comparable_periods"],
                    "strength": "low",
                    "wording_limit": "insufficient",
                }
            ],
        }

        self.assertEqual(_route_after_next_action(state), "degrade")
        self.assertEqual(state["next_action"]["next_action"], "degrade")
        self.assertEqual(
            state["checkpoint_events"][-1]["route"],
            "ask_overridden_to_degrade",
        )

    def test_next_action_ask_degrades_when_post_evidence_gap_is_not_business_ambiguity(self):
        state = {
            "request": {"allow_question_interrupt": True},
            "checkpoint_events": [{"node": "decide_next_action"}],
            "next_action": {
                "next_action": "ask_question",
                "decision_summary": "建议用户补充更多业务维度。",
            },
            "intent": {
                "ambiguous_slots": [],
                "pattern_family": "rolling",
                "question_family": "revenue_health_review",
                "target_metric": "paid_amount",
                "scope": "all_users",
                "time_window": "2026-01-01..2026-06-30",
            },
            "evidence_brief": {
                "limitations": ["driver_components_missing"],
                "primary_capability": "driver_decomposition",
                "wording_limit": "insufficient",
            },
            "evidence": [
                {
                    "capability_id": "data_quality_profile",
                    "evidence_ref": "data_quality_profile:run",
                    "strength": "high",
                    "wording_limit": "supported",
                    "limitations": [],
                },
                {
                    "capability_id": "driver_decomposition",
                    "evidence_ref": "driver_decomposition:inline",
                    "strength": "low",
                    "wording_limit": "insufficient",
                    "limitations": ["driver_components_missing"],
                },
            ],
        }

        self.assertEqual(_route_after_next_action(state), "degrade")
        self.assertEqual(state["next_action"]["next_action"], "degrade")
        self.assertEqual(
            state["checkpoint_events"][-1]["route"],
            "ask_overridden_to_degrade",
        )

    def test_llm_narrative_rejects_non_string_narrative_values(self):
        with self.assertRaisesRegex(
            LLMOutputError,
            "llm_narrative_invalid:evidence_boundary",
        ):
            _localize_narrative_fields(
                {
                    "evidence_boundary": {
                        "weekday_calendar_compare": "medium",
                        "event_evidence": "low",
                    }
                }
            )

    def test_llm_narrative_localizes_audit_issue_descriptions(self):
        output = _localize_narrative_fields(
            {
                "issues": [
                    {
                        "description": (
                            "draft_claims and evidence_brief disagree with wording_limit "
                            "for paid_amount."
                        ),
                        "issue_description": (
                            "draft_claims and evidence_brief disagree with wording_limit "
                            "for paid_amount."
                        ),
                    }
                ]
            }
        )

        description = output["issues"][0]["description"]
        issue_description = output["issues"][0]["issue_description"]
        self.assertNotIn("draft_claims", description)
        self.assertNotIn("evidence_brief", description)
        self.assertNotIn("wording_limit", description)
        self.assertNotIn("paid_amount", description)
        self.assertNotIn("draft_claims", issue_description)
        self.assertNotIn("evidence_brief", issue_description)
        self.assertNotIn("wording_limit", issue_description)
        self.assertNotIn("paid_amount", issue_description)
        self.assertIn("答案声明", description)
        self.assertIn("证据摘要", description)

    def test_business_question_terminal_nodes_fail_without_local_fallback(self):
        class TimeoutOnTerminalNodeLLM(ScriptedLLMClient):
            def __init__(self, failing_task):
                super().__init__({})
                self.failing_task = failing_task

            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                if task == self.failing_task:
                    self.calls.append(task)
                    raise TimeoutError("llm_response_timeout")
                return super().invoke_json(
                    task=task,
                    prompt_version=prompt_version,
                    messages=messages,
                    required_keys=required_keys,
                )

        for task, node in (
            ("degraded_explanation", workflow_module._generate_degraded_explanation),
            ("blocked_explanation", workflow_module._generate_blocked_explanation),
        ):
            with self.subTest(task=task):
                fake = TimeoutOnTerminalNodeLLM(task)
                state = {
                    "request": {"run_mode": "production"},
                    "run_id": f"{task}-timeout",
                    "intent": {"target_metric": "paid_amount"},
                    "evidence_brief": {},
                    "verifier": {},
                    "validator_results": [],
                    "llm_client": fake,
                    "llm_calls": [],
                }

                with self.assertRaisesRegex(WorkflowFailure, "llm_response_timeout"):
                    node(state)

                self.assertEqual(fake.calls, [task])


    def test_empty_final_business_summary_fails_after_one_node_call(self):
        state = {
            "request": {"question": "昨天的收入证据充分吗？"},
            "intent": {"pattern_family": ""},
            "draft_claims": [],
        }
        payloads = []

        def summarize(_state, task, payload, **_kwargs):
            self.assertEqual(task, "final_business_summary")
            payloads.append(payload)
            if len(payloads) == 1:
                return {
                    "summary_text": "",
                    "statement_bindings": [],
                    "display_summary": "",
                }
            return {
                "summary_text": "当前数据证据不足，需要检查支付状态数据。",
                "statement_bindings": [],
                "display_summary": "当前数据证据不足。",
            }

        with patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
            side_effect=summarize,
        ):
            with self.assertRaisesRegex(
                WorkflowFailure,
                "final_business_summary_contract_invalid:summary_text",
            ):
                _final_business_summary(state)

        self.assertEqual(len(payloads), 1)
        self.assertNotIn("final_answer_retry_instruction", payloads[0])

    def test_final_business_summary_rejects_non_string_or_untrimmed_text(self):
        for summary_text in ({"text": "摘要"}, 42, " 摘要 "):
            state = {
                "request": {"question": "昨天的收入证据充分吗？"},
                "intent": {"pattern_family": ""},
                "draft_claims": [],
            }
            with self.subTest(summary_text=summary_text), patch(
                "bi_agent.runtime.langgraph_workflow._invoke_llm",
                return_value={
                    "summary_text": summary_text,
                    "statement_bindings": [],
                    "display_summary": "摘要格式检查。",
                },
            ), self.assertRaisesRegex(
                WorkflowFailure,
                "final_business_summary_contract_invalid:summary_text",
            ):
                _final_business_summary(state)

    def test_final_answer_audit_provider_cannot_return_status_or_hard_blocker(self):
        state = _required_claim_resolution_state()
        summary = "已验证结论保持原样。"
        fake = ScriptedLLMClient(
            {
                "final_answer_audit": {
                    "display_status": "hard_blocked",
                    "hard_blockers": ["unsupported_main_claim"],
                    "repairable_warnings": [],
                    "retry_instruction": "请降低原因表述强度。",
                    "business_audit_summary": "文案存在一处证据边界风险。",
                    "display_summary": "文案存在一处证据边界风险。",
                }
            }
        )
        state.update(
            {
                "run_id": "provider-final-audit-advisory",
                "llm_client": fake,
                "llm_calls": [],
                "final_business_summary": summary,
                "validator_results": [],
                "verifier": {"status": "passed", "errors": []},
            }
        )
        _reduce_evidence(state)
        state["draft_claims"] = workflow_module._authority_claims_from_evidence(state)
        state["authority_verified_claims"] = deepcopy(state["draft_claims"])

        with self.assertRaisesRegex(
            WorkflowFailure,
            "final_answer_audit_top_level_contract_invalid",
        ):
            workflow_module._final_answer_audit(state)

        self.assertEqual(state["final_business_summary"], summary)

    def test_answer_synthesis_receives_business_causal_boundary_only(self):
        state = _required_claim_resolution_state()
        fake = ScriptedLLMClient(
            {
                "causal_audit": {
                    "causal_assessment": "not_supported",
                    "publishable_wording": "会计贡献可保留，业务机制尚未验证。",
                    "supporting_reasons": ["当前缺少独立机制证据。"],
                    "evidence_limit": "当前只能发布已验证的会计分解。",
                    "display_summary": "结论保留业务机制边界。",
                },
                "answer_synthesis": {
                    "answer_text": "已保留当前可验证的业务事实。",
                    "display_summary": "已形成业务回答。",
                },
            }
        )
        state.update(
            {
                "run_id": "answer-business-causal-boundary",
                "llm_client": fake,
                "llm_calls": [],
            }
        )
        _reduce_evidence(state)
        workflow_module._audit_causal_implications(state)

        workflow_module._synthesize_answer(state)

        payload = _llm_input_payload(
            {"admin_audit": {"llm_calls": state["llm_calls"]}},
            "answer_synthesis",
        )
        self.assertEqual(set(payload), {"businessContext"})
        self.assertIn("causalBoundary", payload["businessContext"])
        self.assertIn("业务机制", payload["businessContext"]["causalBoundary"])
        visible = json.dumps(payload, ensure_ascii=False)
        for internal in (
            "causal_evidence_dossier",
            "causal_audit",
            "causal_assessment",
            "candidate_hypothesis",
            "evidence_ref",
        ):
            self.assertNotIn(internal, visible)

    def test_causal_audit_receives_business_projection_and_keeps_dossier_local(self):
        state = _required_claim_resolution_state()
        fake = ScriptedLLMClient(
            {
                "causal_audit": {
                    "causal_assessment": "not_supported",
                    "publishable_wording": "会计贡献可保留，业务机制尚未验证。",
                    "supporting_reasons": ["当前缺少独立机制证据。"],
                    "evidence_limit": "当前只能发布已验证的会计分解。",
                    "display_summary": "结论保留业务机制边界。",
                }
            }
        )
        state.update(
            {
                "run_id": "causal-audit-business-projection",
                "llm_client": fake,
                "llm_calls": [],
            }
        )
        _reduce_evidence(state)

        workflow_module._audit_causal_implications(state)

        self.assertEqual(
            state["causal_audit"]["causal_assessment"],
            "not_supported",
        )
        self.assertNotIn(
            "alternative_explanations",
            state["causal_audit"],
        )
        self.assertIn("observed_pattern", state["causal_evidence_dossier"])
        payload = _llm_input_payload(
            {"admin_audit": {"llm_calls": state["llm_calls"]}},
            "causal_audit",
        )
        self.assertEqual(set(payload), {"businessContext", "causalReview"})
        visible = json.dumps(payload, ensure_ascii=False)
        for internal in (
            "causal_evidence_dossier",
            "observed_pattern",
            "evidence_ref",
            "capability_id",
            "wording_limit",
        ):
            self.assertNotIn(internal, visible)

    def test_business_intent_prompt_does_not_prebind_question_family(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        payload = workflow_module._business_intent_payload(
            {
                "question": "2026年Q2相比Q1，付费金额有没有明显变化？",
                "question_family": "pattern_explanation",
                "pattern_family": "custom_baseline",
                "scope": "full_sample",
                "time_window": "2026-01-01..2026-06-30",
            },
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )

        self.assertNotIn("question_family_hint", payload)
        self.assertNotIn("question_family", payload.get("bound_business_context", {}))

    def test_execute_capabilities_runs_public_custom_baseline_graph(self):
        compiled = compile_graph(
            question_family="custom_baseline_comparison",
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=(
                "data_quality_profile",
                "compare_periods",
                "evidence_reduce",
                "answer_verify",
            ),
        )
        state = {
            "request": {
                "rows": [
                    {"period": "h1_2026", "group": "baseline", "amount": 100},
                    {"period": "h1_2026", "group": "target", "amount": 120},
                ],
                "required_fields": ("period", "group", "amount"),
            },
            "run_id": "execute-public-custom-baseline",
            "sql_hash": "sqlhash-custom",
            "budget_state": default_budget("ordinary"),
            "compiled_graph": compiled,
            "intent": {
                "question_family": "custom_baseline_comparison",
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "pattern_params": {
                    "period_key": "period",
                    "group_key": "group",
                    "target_group": "target",
                    "baseline_group": "baseline",
                    "min_periods": 1,
                },
                "scope": "full_sample",
                "time_window": "2026-01-01..2026-06-30",
                "target_claim": "Q2 相比 Q1 的付费金额变化",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
            },
        }

        result = _execute_capabilities(state)

        evidence = result["evidence"]
        self.assertTrue(
            any(item.get("capability_id") == "compare_periods" for item in evidence)
        )
        self.assertTrue(
            any(item.get("capability_id") == "data_quality_profile" for item in evidence)
        )

    def test_production_execution_does_not_insert_intra_period_target_phase(self):
        from types import SimpleNamespace

        for run_mode in ("production", "live"):
            with self.subTest(run_mode=run_mode):
                compared_params = []
                scanned_params = []

                def execute(request):
                    compared_params.append(dict(request.params))
                    return {
                        "evidence_ref": f"{request.capability_id}:evidence",
                        "capability_id": request.capability_id,
                        "typed_payload": {},
                        "result_refs": (),
                    }

                def scan(rows, **params):
                    scanned_params.append(dict(params))
                    return {
                        "evidence_ref": "pattern_scan:evidence",
                        "capability_id": "pattern_scan",
                        "typed_payload": {},
                        "result_refs": (),
                    }

                state = {
                    "request": {
                        "run_mode": run_mode,
                    },
                    "run_id": f"no-intra-period-default-{run_mode}",
                    "sql_hash": "",
                    "budget_state": default_budget("ordinary"),
                    "compiled_graph": SimpleNamespace(
                        mutations=SimpleNamespace(
                            accepted_graph=("compare_period_phases", "pattern_scan")
                        )
                    ),
                    "intent": {
                        "question_family": "pattern_explanation",
                        "target_metric": "paid_amount",
                        "pattern_family": "intra_period",
                        "pattern_params": {},
                        "scope": "full_sample",
                        "time_window": "2026-06-02",
                        "target_claim": "检查周期内阶段变化",
                    },
                }

                with patch(
                    "bi_agent.runtime.langgraph_workflow.execute_capability",
                    side_effect=execute,
                ), patch(
                    "bi_agent.runtime.langgraph_workflow.scan_pattern",
                    side_effect=scan,
                ):
                    _execute_capabilities(state)

                self.assertEqual(len(compared_params), 1)
                self.assertEqual(len(scanned_params), 1)
                self.assertNotIn("target_phase", compared_params[0])
                self.assertNotIn("target_phase", scanned_params[0])

    def test_execute_capabilities_dispatches_reviewed_runtime_bound_capability(self):
        from types import SimpleNamespace

        bound = object()
        state = {
            "request": {
                "runtime_rows_source": "analysis_runtime",
                "bound_capability_inputs": {"market_health_compare": bound},
            },
            "run_id": "execute-runtime-market",
            "budget_state": default_budget("ordinary"),
            "compiled_graph": SimpleNamespace(
                mutations=SimpleNamespace(
                    accepted_graph=("compare_periods", "market_health_compare")
                )
            ),
            "intent": {
                "question_family": "custom_baseline_comparison",
                "target_metric": "active_users",
                "pattern_family": "custom_baseline",
                "scope": "full_sample",
                "time_window": "昨天与前天",
                "target_claim": "comparative_change",
                "baseline": {"label": "前天"},
                "target": {"label": "昨天"},
            },
        }
        captured = []

        def execute(request):
            captured.append(request)
            return {
                "evidence_ref": "market-health:evidence",
                "capability_id": request.capability_id,
                "typed_payload": {},
                "numeric_facts": {},
                "result_refs": (),
            }

        with patch(
            "bi_agent.runtime.langgraph_workflow.execute_capability",
            side_effect=execute,
        ):
            result = _execute_capabilities(state)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].capability_id, "market_health_compare")
        self.assertIs(captured[0].bound_input, bound)
        self.assertEqual(result["evidence"][0]["capability_id"], "market_health_compare")

    def test_production_capability_cannot_bypass_blocked_bound_input_with_compatibility_rows(self):
        from types import SimpleNamespace
        from bi_agent.runtime.capability_execution import BoundCapabilityInput

        bound = object.__new__(BoundCapabilityInput)
        for field in BoundCapabilityInput.__annotations__:
            if field in {"rows_by_slot", "binding_manifest"}:
                value = {}
            elif field in {"maximum_claim_strength_rank"}:
                value = -1
            elif field.endswith("s") or field.endswith("refs"):
                value = ()
            else:
                value = ""
            object.__setattr__(bound, field, value)
        object.__setattr__(bound, "capability_id", "driver_decomposition")
        object.__setattr__(bound, "status", "blocked")
        object.__setattr__(bound, "reasons", ("missing_required_query_slot",))

        state = {
            "request": {
                "run_mode": "production",
                "runtime_rows_source": "analysis_runtime",
                "bound_capability_inputs": {"driver_decomposition": bound},
                "runtime_rows_by_intent": {
                    "component_driver_scan": [
                        {"group": "baseline", "amount": 1, "paid_users": 1},
                        {"group": "target", "amount": 1_000_000, "paid_users": 1},
                    ]
                },
                "result_refs_by_intent": {
                    "component_driver_scan": ("result:compatibility-map",)
                },
            },
            "run_id": "blocked-driver-cannot-bypass",
            "sql_hash": "",
            "budget_state": default_budget("ordinary"),
            "compiled_graph": SimpleNamespace(
                mutations=SimpleNamespace(accepted_graph=("driver_decomposition",))
            ),
            "intent": {
                "question_family": "paid_amount_change_explanation",
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "pattern_params": {"group_key": "group", "target_group": "target"},
                "scope": "full_sample",
                "time_window": "昨天与前天",
                "target_claim": "formula_component_contribution",
                "baseline": {"label": "前天"},
                "target": {"label": "昨天"},
            },
        }

        evidence = _execute_capabilities(state)["evidence"][0]

        self.assertEqual(evidence["input_status"], "blocked")
        self.assertFalse(_evidence_established(evidence))
        self.assertNotIn(999999.0, evidence.get("numeric_facts", {}).values())
        self.assertNotIn("result:compatibility-map", evidence.get("result_refs", ()))

    def test_production_segment_bridge_and_joint_use_exact_bound_slots(self):
        from types import SimpleNamespace
        from bi_agent.runtime.capability_execution import BoundCapabilityInput

        def ready_bound(capability_id, slot, rows, result_ref, claim_type):
            bound = object.__new__(BoundCapabilityInput)
            for field in BoundCapabilityInput.__annotations__:
                if field in {"rows_by_slot", "binding_manifest"}:
                    value = {}
                elif field == "maximum_claim_strength_rank":
                    value = 2
                elif field.endswith("s") or field.endswith("refs"):
                    value = ()
                else:
                    value = ""
                object.__setattr__(bound, field, value)
            object.__setattr__(bound, "capability_id", capability_id)
            object.__setattr__(bound, "status", "ready")
            object.__setattr__(bound, "rows_by_slot", {slot: tuple(rows)})
            object.__setattr__(bound, "result_refs", (result_ref,))
            object.__setattr__(bound, "supported_claim_types", (claim_type,))
            object.__setattr__(
                bound,
                "supported_evidence_types",
                ("contextual_evidence", "statistical_association"),
            )
            object.__setattr__(bound, "maximum_claim_strength", "candidate_driver")
            object.__setattr__(bound, "claim_strength_taxonomy_version", "v1")
            object.__setattr__(bound, "input_completeness_statuses", ("complete",))
            object.__setattr__(bound, "binding_manifest_ref", f"binding:{capability_id}")
            object.__setattr__(bound, "binding_manifest_digest", f"digest:{capability_id}")
            return bound

        segment = ready_bound(
            "segment_bridge",
            "dimension_contribution_scan",
            ({"segment": "actual", "amount": 500.0, "n": 50},),
            "result:segment-bound",
            "segment_contribution_or_mix_shift",
        )
        joint = ready_bound(
            "joint_attribution",
            "joint_candidate_scan",
            (
                {"group": "baseline", "channel": "ads", "method": "card", "amount": 100.0, "n": 50},
                {"group": "target", "channel": "ads", "method": "card", "amount": 180.0, "n": 50},
            ),
            "result:joint-bound",
            "candidate_driver",
        )
        state = {
            "request": {
                "run_mode": "production",
                "runtime_rows_source": "analysis_runtime",
                "bound_capability_inputs": {
                    "segment_bridge": segment,
                    "joint_attribution": joint,
                },
                "segments": ({"segment": "full_sample", "amount": 1.0, "n": 100},),
            },
            "run_id": "bound-segment-joint",
            "sql_hash": "",
            "budget_state": default_budget("ordinary"),
            "compiled_graph": SimpleNamespace(
                mutations=SimpleNamespace(
                    accepted_graph=("segment_bridge", "joint_attribution")
                )
            ),
            "intent": {
                "question_family": "segment_or_factor_attribution",
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "pattern_params": {
                    "group_key": "group",
                    "target_group": "target",
                    "baseline_group": "baseline",
                },
                "scope": "full_sample",
                "time_window": "昨天与前天",
                "target_claim": "candidate_driver",
                "baseline": {"label": "前天"},
                "target": {"label": "昨天"},
            },
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow.validate_bound_capability_input",
            return_value="",
        ):
            evidence = _execute_capabilities(state)["evidence"]

        segment_evidence = next(
            item for item in evidence if item["capability_id"] == "segment_bridge"
        )
        joint_evidence = next(
            item for item in evidence if item["capability_id"] == "joint_attribution"
        )
        self.assertEqual(
            segment_evidence["typed_payload"]["segments"][0]["segment"],
            "actual",
        )
        self.assertEqual(segment_evidence["result_refs"], ("result:segment-bound",))
        self.assertNotEqual(
            joint_evidence["typed_payload"].get("reason"),
            "no_escalation_required",
        )
        self.assertEqual(
            joint_evidence["typed_payload"]["dimension_keys"],
            ["channel", "method"],
        )

    def test_production_formula_decompose_uses_declared_formula_candidates(self):
        from types import SimpleNamespace
        from bi_agent.runtime.capability_execution import BoundCapabilityInput

        bound = object.__new__(BoundCapabilityInput)
        for field in BoundCapabilityInput.__annotations__:
            if field in {"rows_by_slot", "binding_manifest"}:
                value = {}
            elif field == "maximum_claim_strength_rank":
                value = 2
            elif field.endswith("s") or field.endswith("refs"):
                value = ()
            else:
                value = ""
            object.__setattr__(bound, field, value)
        object.__setattr__(bound, "capability_id", "formula_decompose")
        object.__setattr__(bound, "status", "ready")
        object.__setattr__(
            bound,
            "rows_by_slot",
            {
                "component_driver_scan": ({
                    "paid_amount": 100.0,
                    "paid_users": 20,
                    "paid_orders": 30,
                    "first_paid_users": 5,
                    "paid_frequency": 1.5,
                    "avg_order_amount": 3.333,
                },)
            },
        )
        object.__setattr__(bound, "result_refs", ("result:formula-bound",))
        object.__setattr__(
            bound,
            "supported_claim_types",
            ("formula_component_contribution",),
        )
        object.__setattr__(bound, "supported_evidence_types", ("accounting_contribution",))
        object.__setattr__(bound, "maximum_claim_strength", "quantified_contribution")
        object.__setattr__(bound, "claim_strength_taxonomy_version", "v1")
        object.__setattr__(bound, "input_completeness_statuses", ("complete",))
        object.__setattr__(bound, "binding_manifest_ref", "binding:formula")
        object.__setattr__(bound, "binding_manifest_digest", "digest:formula")
        state = {
            "request": {
                "run_mode": "production",
                "runtime_rows_source": "analysis_runtime",
                "bound_capability_inputs": {"formula_decompose": bound},
            },
            "run_id": "bound-formula",
            "sql_hash": "",
            "budget_state": default_budget("ordinary"),
            "compiled_graph": SimpleNamespace(
                mutations=SimpleNamespace(accepted_graph=("formula_decompose",))
            ),
            "intent": {
                "question_family": "paid_amount_change_explanation",
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "scope": "full_sample",
                "time_window": "昨天与前天",
                "target_claim": "formula_component_contribution",
                "requested_components": [
                    "first_paid_users",
                    "paid_frequency",
                    "avg_order_amount",
                    "payment_success_rate",
                ],
            },
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow.validate_bound_capability_input",
            return_value="",
        ):
            formula = _execute_capabilities(state)["evidence"][0]

        self.assertEqual(
            formula["evidence_ref"],
            "formula_decompose:bound-formula",
        )
        payload = formula["typed_payload"]
        path = next(
            item
            for item in payload["covered_paths"]
            if item["formula_id"] == "frequency_ticket_size"
        )
        self.assertEqual(
            path["components"],
            [
                "paid_users",
                "paid_frequency",
                "avg_order_amount",
            ],
        )
        self.assertEqual(path["candidate_role"], "primary_candidate")
        self.assertIsNone(payload["primary_formula"])
        self.assertEqual(payload["selection_state"], "candidate_only")
        self.assertIn(
            "payment_success_chain",
            {
                item["formula_id"]
                for item in (*payload["covered_paths"], *payload["gaps"])
            },
        )
        self.assertEqual(formula["result_refs"], ("result:formula-bound",))

    def test_production_degraded_explanation_does_not_create_local_claim(self):
        from types import SimpleNamespace
        from bi_agent.runtime.langgraph_workflow import _ensure_degraded_audit

        state = {
            "request": {"run_mode": "production"},
            "run_id": "production-degraded-no-local-claim",
            "intent": {
                "scope": "full_sample",
                "time_window": "2026-06-02",
                "target_metric": "paid_amount",
            },
            "analysis_runtime_result": SimpleNamespace(status="degraded"),
            "evidence": [],
            "draft_claims": [],
        }

        _ensure_degraded_audit(state)

        self.assertEqual(state["draft_claims"], [])

    def test_typed_runtime_records_real_clickhouse_validator_without_phase4_placeholder(self):
        from types import SimpleNamespace

        runtime_result = SimpleNamespace(
            analysis_contract=object(),
            query_results=(
                SimpleNamespace(
                    execution_status="succeeded",
                    result_ref="result:typed",
                    query_contract_ref="query:typed",
                ),
            ),
            query_contracts=(),
            capability_plans=(
                SimpleNamespace(
                    capability_id="compare_periods",
                    required_input_slots=(
                        SimpleNamespace(
                            query_contract_refs=("query:typed",),
                            validation_query_contract_refs=(),
                        ),
                    ),
                    optional_input_slots=(),
                ),
            ),
            capability_roles={"compare_periods": "required"},
            bound_capability_inputs={},
            repair_decisions=(),
            to_workflow_payload=lambda: {
                "runtime_rows_by_intent": {
                    "daily_metric_baselines": [{"window_role": "target"}]
                },
                "result_refs_by_intent": {
                    "daily_metric_baselines": ["result:typed"]
                },
            },
        )
        runtime = SimpleNamespace(execute=lambda _request: runtime_result)
        state = {
            "request": {
                "analysis_runtime": runtime,
                "run_mode": "production",
            },
            "run_id": "typed-validator",
            "checkpoint_events": [],
            "intent": {"pattern_family": "custom_baseline"},
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow._analysis_runtime_request",
            return_value=SimpleNamespace(accepted_graph=()),
        ), patch(
            "bi_agent.runtime.langgraph_workflow._record_execution_material"
        ):
            _validate_runtime_binding(state)
            _fetch_runtime_rows(state)

        validators = state["validator_results"]
        self.assertFalse(
            any(item.get("reason") == "phase4_draft_binding" for item in validators)
        )
        self.assertIn(
            {
                "validator": "clickhouse_runtime",
                "ok": True,
                "reason": "provider_rows_loaded",
                "result_refs": ["result:typed"],
            },
            validators,
        )

    def test_clickhouse_validator_keeps_primary_success_when_auxiliary_query_fails(self):
        from types import SimpleNamespace

        def slot(query_ref):
            return SimpleNamespace(
                query_contract_refs=(query_ref,),
                validation_query_contract_refs=(),
            )

        runtime_result = SimpleNamespace(
            analysis_contract=object(),
            query_results=(
                SimpleNamespace(
                    execution_status="succeeded",
                    result_ref="result:primary",
                    query_contract_ref="query:primary",
                ),
                SimpleNamespace(
                    execution_status="failed",
                    result_ref="result:context",
                    query_contract_ref="query:context",
                ),
            ),
            query_contracts=(),
            capability_plans=(
                SimpleNamespace(
                    capability_id="driver_decomposition",
                    required_input_slots=(slot("query:primary"),),
                    optional_input_slots=(),
                ),
                SimpleNamespace(
                    capability_id="rolling_window_compare",
                    required_input_slots=(slot("query:context"),),
                    optional_input_slots=(),
                ),
            ),
            capability_roles={
                "driver_decomposition": "required",
                "rolling_window_compare": "auxiliary",
            },
            bound_capability_inputs={},
            repair_decisions=(),
            to_workflow_payload=lambda: {
                "runtime_rows_by_intent": {
                    "component_driver_scan": [{"window_role": "target"}]
                },
                "result_refs_by_intent": {
                    "component_driver_scan": ["result:primary"]
                },
            },
        )
        runtime = SimpleNamespace(execute=lambda _request: runtime_result)
        state = {
            "request": {
                "analysis_runtime": runtime,
                "run_mode": "production",
            },
            "run_id": "typed-validator-auxiliary-failure",
            "checkpoint_events": [],
            "intent": {"pattern_family": "custom_baseline"},
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow._analysis_runtime_request",
            return_value=SimpleNamespace(accepted_graph=()),
        ), patch(
            "bi_agent.runtime.langgraph_workflow._record_execution_material"
        ):
            _validate_runtime_binding(state)
            _fetch_runtime_rows(state)

        self.assertIn(
            {
                "validator": "clickhouse_runtime",
                "ok": True,
                "reason": "primary_rows_loaded_with_auxiliary_limits",
                "result_refs": ["result:primary", "result:context"],
            },
            state["validator_results"],
        )

    def test_unbound_or_over_ceiling_directional_evidence_is_not_established(self):
        base = {
            "evidence_type": "statistical_association",
            "strength": "directional",
            "wording_limit": "quantified",
            "input_status": "ready",
            "binding_manifest_ref": "capability-binding:market:1",
            "claim_type": "comparative_change",
            "supported_claim_types": ("comparative_change",),
            "supported_evidence_types": ("statistical_association",),
            "maximum_claim_strength": "directional",
            "limitations": (),
        }
        variants = (
            {**base, "binding_manifest_ref": ""},
            {**base, "input_status": "degraded"},
            {**base, "limitations": ("partial",)},
            {**base, "supported_claim_types": ("source_reconciliation",)},
            {**base, "maximum_claim_strength": "trust_boundary"},
        )

        for evidence in variants:
            with self.subTest(evidence=evidence):
                self.assertFalse(_evidence_established(evidence))

    def test_provider_degrade_keeps_publishable_required_claims_alive(self):
        state = _current_required_claim_resolution_state()
        _reduce_evidence(state)
        self.assertEqual(
            state["evidence_brief"]["required_claim_evidence_refs"],
            {
                "comparative_change": "compare_periods:ready",
                "formula_component_contribution": "driver_decomposition:ready",
            },
        )
        state["next_action"] = {
            "next_action": "degrade",
            "decision_summary": "当前证据不足。",
        }

        route = _route_after_next_action(state)

        self.assertEqual(route, "synthesize")
        self.assertEqual(state["next_action"]["next_action"], "synthesize_answer")
        self.assertEqual(
            state["checkpoint_events"][-1]["route"],
            "degrade_overridden_to_bounded_answer",
        )

    def test_missing_payment_success_observation_keeps_core_driver_claim_publishable(self):
        state = _current_required_claim_resolution_state()

        _reduce_evidence(state)

        driver = next(
            item
            for item in state["evidence"]
            if item["capability_id"] == "driver_decomposition"
        )
        assumption = driver["typed_payload"]["decompositions"][0][
            "payment_success_assumption"
        ]
        core_factors = tuple(
            item["component_id"]
            for item in driver["typed_payload"]["decompositions"][0][
                "core_factor_contributions"
            ]
        )
        claim = _default_claim_from_evidence(state)

        self.assertEqual(
            core_factors,
            ("avg_order_amount", "paid_frequency", "paid_users"),
        )
        self.assertFalse(assumption["observed"])
        self.assertEqual(assumption["status"], "assumed_neutral")
        self.assertTrue(driver["claim_input_ready"])
        self.assertEqual(driver["wording_limit"], "quantified")
        self.assertEqual(driver["limitations"], [])
        self.assertTrue(workflow_module._evidence_supports_bounded_answer(state))
        self.assertEqual(
            state["evidence_brief"]["required_claim_evidence_refs"][
                "formula_component_contribution"
            ],
            "driver_decomposition:ready",
        )
        self.assertNotIn(
            "payment_success",
            " ".join(state["evidence_brief"]["limitations"]),
        )
        self.assertIn("支付成功率缺少独立观测", claim["text"])
        self.assertIn("按不变处理", claim["text"])
        self.assertNotIn("已观测", claim["text"])
        self.assertNotIn("没有影响", claim["text"])
        self.assertNotIn("无影响", claim["text"])
        self.assertNotIn("100%", claim["text"])

    def test_default_claim_renders_bound_target_vs_baseline_comparison(self):
        state = _required_claim_resolution_state()
        state["intent"]["required_claim_intents"] = ["comparative_change"]
        state["intent"]["candidate_claim_intents"] = []
        state["evidence"] = [state["evidence"][0]]
        _reduce_evidence(state)

        claim = _default_claim_from_evidence(state)

        self.assertEqual(claim["evidence_refs"], ["compare_periods:ready"])
        self.assertIn("2026-06-01", claim["text"])
        self.assertIn("2026-05-31", claim["text"])
        self.assertIn("上涨", claim["text"])
        self.assertIn("1.35%", claim["text"])

    def test_route_normalization_preserves_joint_attribution_for_combination_asks(self):
        normalized = _normalize_route_requested_nodes(
            ("joint_attribution", "answer_verify"),
            {
                "question_family": "segment_or_factor_attribution",
                "target_claim": "判断渠道和月内阶段组合是否解释主要变化",
                "pattern_params": {"joint_dimension_keys": ("channel", "phase")},
                "analysis_requirements": {"requested_dimensions": ["channel", "phase"]},
            },
        )

        self.assertIn("joint_attribution", normalized)
        self.assertIn("data_quality_profile", normalized)

    def test_answer_synthesis_context_includes_capability_business_findings(self):
        state = {
            "intent": {
                "question_family": "custom_baseline_comparison",
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "analysis_requirements": {
                    "requested_dimensions": ["channel"],
                    "diagnostic_tags": ["pattern_attribution"],
                },
                "scope": "full_sample",
                "time_window": "2026-01-01..2026-06-30",
                "target_claim": "去掉异常后方向是否还成立",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
            },
            "request": {"question": "去掉异常后还成立吗？"},
            "evidence_brief": {"limitations": []},
            "causal_evidence_dossier": {},
            "causal_audit": {},
            "evidence": [
                {
                    "evidence_ref": "outlier_contribution:inline",
                    "capability_id": "outlier_contribution",
                    "strength": "medium",
                    "wording_limit": "contextual",
                    "typed_payload": {
                        "business_readout": "移除最大正向日期后，方向仍为上升。",
                        "claim_boundary": "只能说明异常敏感性，不能当作因果证明。",
                        "top_positive_share": 0.6,
                        "remaining_delta_after_top_positive": 20.0,
                    },
                    "limitations": [],
                    "result_refs": ["sqlhash-1"],
                }
            ],
        }

        context = _answer_synthesis_context(state)

        self.assertEqual(
            context["capability_business_findings"],
            [
                {
                    "capability": "outlier_contribution",
                    "business_readout": "移除最大正向日期后，方向仍为上升。",
                    "claim_boundary": "只能说明异常敏感性，不能当作因果证明。",
                    "evidence_refs": ["sqlhash-1"],
                }
            ],
        )

    def test_answer_synthesis_context_includes_claim_slots_and_bounded_insight(self):
        state = {
            "intent": {
                "question_family": "custom_baseline_comparison",
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2026-01-01..2026-06-30",
                "target_claim": "Q2 相比 Q1 为什么变化",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
            },
            "request": {"question": "Q2 相比 Q1 为什么变化？"},
            "evidence_brief": {"limitations": ["weak_direction"]},
            "causal_evidence_dossier": {},
            "causal_audit": {},
            "draft_claims": [
                {
                    "text": "Q2 相比 Q1 的付费金额提升 20.0%，当前只支持窗口对比结论。",
                    "numbers": {"change_pct": 0.2},
                    "scope": "full_sample",
                    "time_window": "2026-01-01..2026-06-30",
                    "claim_strength": "observed",
                    "evidence_refs": ["compare_periods:inline"],
                }
            ],
            "verifier": {"errors": []},
            "evidence": [
                {
                    "evidence_ref": "compare_periods:inline",
                    "capability_id": "compare_periods",
                    "strength": "medium",
                    "wording_limit": "contextual",
                    "typed_payload": {"median_uplift": 0.2},
                    "limitations": ["weak_direction"],
                }
            ],
        }

        context = _answer_synthesis_context(state)

        self.assertEqual(
            context["verified_claim_slots"][0]["business_claim"],
            "Q2 相比 Q1 的付费金额提升 20.0%，当前只支持窗口对比结论。",
        )
        self.assertIn("排查方向", context["bounded_insight_guidance"]["insight_prompt"])
        self.assertIn("方向一致性不足", context["bounded_insight_guidance"]["evidence_limits"])


    def test_route_normalization_adds_answer_verify_for_change_reason_questions(self):
        nodes = _normalize_route_requested_nodes(
            ("driver_decomposition",),
            {
                "question": "Q2 相比 Q1 付费金额为什么变了？",
                "question_family": "paid_amount_change_explanation",
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "analysis_requirements": {
                    "requested_dimensions": ["channel"],
                    "diagnostic_tags": ["pattern_attribution"],
                },
            },
        )

        self.assertIn("driver_decomposition", nodes)

    def test_route_normalization_adds_answer_verify_for_main_reason_attribution(self):
        nodes = _normalize_route_requested_nodes(
            ("pattern_scan",),
            {
                "question": "这些渠道里 WajeSpecial 是主要原因吗？",
                "question_family": "pattern_explanation",
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
            },
        )

        self.assertIn("evidence_reduce", nodes)
        self.assertIn("answer_verify", nodes)

    def test_route_normalization_keeps_llm_requested_segment_for_compiler_audit(self):
        nodes = _normalize_route_requested_nodes(
            ("driver_decomposition", "segment_contribution", "answer_verify"),
            {
                "question_family": "segment_or_factor_attribution",
                "pattern_family": "custom_baseline",
                "target_claim": "Q2提升主要是付费用户数贡献还是单付费用户金额贡献",
                "target_metric": "paid_amount",
                "analysis_requirements": {"requested_dimensions": ["channel"]},
            },
        )

        self.assertIn("driver_decomposition", nodes)
        self.assertIn("segment_contribution", nodes)




    def test_route_normalization_keeps_compare_and_verify_for_daily_average_corrections(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile", "driver_decomposition"),
            {
                "question": "换成日均再看一遍。",
                "question_family": "custom_baseline_comparison",
                "primary_question_family": "custom_baseline_comparison",
                "pattern_family": "custom_baseline",
                "target_claim": "按日均付费金额重新比较 Q2 和 Q1",
                "target_metric": "paid_amount",
                "analysis_requirements": {"baselines": ["rolling_7_day_baseline"]},
            },
        )

        self.assertIn("compare_periods", nodes)
        self.assertIn("answer_verify", nodes)

    def test_route_normalization_keeps_compare_and_verify_for_weekly_corrections(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile",),
            {
                "question": "口径改成按周看，还一样吗？",
                "question_family": "pattern_explanation",
                "primary_question_family": "pattern_explanation",
                "pattern_family": "rolling",
                "target_claim": "按周粒度复核付费金额方向",
                "target_metric": "paid_amount",
                "analysis_requirements": {"baselines": ["rolling_7_day_baseline"]},
            },
        )

        self.assertIn("compare_periods", nodes)
        self.assertIn("formula_decompose", nodes)

    def test_business_intent_preserves_llm_pattern_params(self):
        fake = ScriptedLLMClient(
            {
                "business_intent": _provider_business_intent_output(
                    question_family="pattern_explanation",
                    pattern_family="weekly",
                    pattern_params={
                        "week_key": "week",
                        "weekday_key": "weekday",
                        "target_weekdays": [6, 7],
                        "baseline_weekdays": [1, 2, 3, 4, 5],
                    },
                )
            }
        )
        state = {
            "request": {"question": "最近付费金额是不是周末更高？"},
            "run_id": "intent-pattern-params",
            "llm_client": fake,
            "llm_calls": [],
        }

        _understand_business_intent(state)

        self.assertEqual(state["intent"]["pattern_family"], "weekly")
        self.assertEqual(state["intent"]["pattern_params"]["target_weekdays"], [6, 7])
        self.assertEqual(state["intent"]["pattern_params"]["baseline_weekdays"], [1, 2, 3, 4, 5])

    def test_route_normalization_keeps_answer_verify_for_actionability_challenges(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile",),
            {
                "question": "这个结果能不能直接指导投放？",
                "question_family": "revenue_health_review",
                "primary_question_family": "revenue_health_review",
                "target_claim": "判断当前结果能否直接指导投放",
                "target_metric": "paid_amount",
                "analysis_requirements": {"claim_intents": ["contract_coverage_and_trust_boundary"]},
            },
        )

        self.assertIn("formula_decompose", nodes)

    def test_route_normalization_keeps_answer_verify_for_stability_challenges(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile",),
            {
                "question": "这些结果有多稳？",
                "question_family": "pattern_explanation",
                "primary_question_family": "pattern_explanation",
                "target_claim": "判断当前结果稳健性",
                "target_metric": "paid_amount",
                "analysis_requirements": {"requested_dimensions": ["channel"]},
            },
        )

        self.assertIn("answer_verify", nodes)

    def test_route_normalization_adds_segment_contribution_when_family_drifts_to_baseline(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile", "compare_periods", "driver_decomposition", "answer_verify"),
            {
                "question": "2026年Q2相比Q1，哪些渠道解释了付费金额变化？",
                "question_family": "custom_baseline_comparison",
                "pattern_family": "custom_baseline",
                "target_claim": "识别各渠道对付费金额变化的解释程度",
                "target_metric": "paid_amount",
            },
        )

        self.assertIn("compare_periods", nodes)

    def test_requested_segment_node_infers_secondary_question_family(self):
        intent = {
            "question_family": "custom_baseline_comparison",
            "primary_question_family": "custom_baseline_comparison",
            "question_families": ["custom_baseline_comparison"],
            "secondary_question_families": [],
        }

        _infer_question_families_from_requested_nodes(
            intent,
            ("data_quality_profile", "segment_contribution", "answer_verify"),
        )

        self.assertIn("segment_or_factor_attribution", intent["question_families"])
        self.assertIn("segment_or_factor_attribution", intent["secondary_question_families"])

    def test_requested_compare_periods_infers_custom_baseline_family(self):
        intent = {
            "question_family": "revenue_health_review",
            "primary_question_family": "revenue_health_review",
            "question_families": ["revenue_health_review"],
            "secondary_question_families": [],
        }

        _infer_question_families_from_requested_nodes(
            intent,
            ("data_quality_profile", "compare_periods", "answer_verify"),
        )

        self.assertIn("custom_baseline_comparison", intent["question_families"])
        self.assertIn("custom_baseline_comparison", intent["secondary_question_families"])

    def test_degrade_override_rewrites_next_action_for_audit(self):
        state = {
            "checkpoint_events": [{"node": "decide_next_action"}],
            "next_action": {
                "next_action": "degrade",
                "decision_summary": "数据缺失，无法回答。",
            },
            "intent": {
                "pattern_family": "custom_baseline",
                "scope": "all_users",
                "time_window": "2026-01-01..2026-06-30",
            },
            "evidence": [
                {
                    "evidence_ref": "joint_attribution:inline",
                    "capability_id": "joint_attribution",
                    "strength": "medium",
                    "wording_limit": "candidate",
                    "typed_payload": {},
                }
            ],
        }

        route = _route_after_next_action(state)

        self.assertEqual(route, "synthesize")
        self.assertEqual(state["next_action"]["next_action"], "synthesize_answer")
        self.assertIn("不能把可回答结果降级", state["next_action"]["decision_summary"])
        self.assertEqual(
            state["checkpoint_events"][-1]["route"],
            "degrade_overridden_to_bounded_answer",
        )

    def test_synthesize_next_action_conflict_text_is_repaired_for_audit(self):
        state = {
            "checkpoint_events": [{"node": "decide_next_action"}],
            "next_action": {
                "next_action": "synthesize_answer",
                "decision_summary": "由于缺少渠道字段，联合归因无法执行。",
                "display_summary": "证据不足以完成归因。",
            },
            "intent": {
                "pattern_family": "custom_baseline",
                "scope": "all_users",
                "time_window": "2026-01-01..2026-06-30",
            },
            "evidence": [
                {
                    "evidence_ref": "joint_attribution:inline",
                    "capability_id": "joint_attribution",
                    "strength": "medium",
                    "wording_limit": "candidate",
                    "typed_payload": {},
                }
            ],
        }

        route = _route_after_next_action(state)

        self.assertEqual(route, "synthesize")
        self.assertIn("继续生成答案", state["next_action"]["decision_summary"])
        self.assertNotIn("无法执行", state["next_action"]["decision_summary"])
        self.assertEqual(
            state["checkpoint_events"][-1]["route"],
            "synthesize_action_text_repaired",
        )

    def test_degraded_explanation_rejects_data_volume_materiality_drift(self):
        state = {
            "evidence_brief": {
                "limitations": ["below_materiality_floor", "weak_direction"]
            },
            "validator_results": [],
        }
        output = {
            "status": "degraded",
            "explanation": "数据量低于重要性门槛，无法解释。",
            "repair_path": "补充更多历史数据。",
        }

        with self.assertRaisesRegex(WorkflowFailure, "materiality_drift"):
            _sanitize_terminal_explanation(output, state, "degraded")

    def test_degraded_explanation_rejects_target_metric_substitution(self):
        state = {
            "intent": {"target_metric": "paid_amount"},
            "evidence_brief": {"limitations": ["source_unbound"]},
            "validator_results": [],
        }
        output = {
            "status": "degraded",
            "explanation": "利润指标所需数据暂时不可用。",
            "repair_path": "补齐数据源后重跑。",
        }

        with self.assertRaisesRegex(WorkflowFailure, "target_metric_drift"):
            _sanitize_terminal_explanation(output, state, "degraded")

    def test_degraded_explanation_rejects_substitution_for_any_bound_metric(self):
        state = {
            "intent": {"target_metric": "payment_success_rate"},
            "evidence_brief": {"limitations": ["source_unbound"]},
            "validator_results": [],
        }
        output = {
            "status": "degraded",
            "explanation": "利润指标所需数据暂时不可用。",
            "repair_path": "补齐数据源后重跑。",
        }

        with self.assertRaisesRegex(WorkflowFailure, "target_metric_drift"):
            _sanitize_terminal_explanation(output, state, "degraded")

    def test_degraded_explanation_allows_target_and_other_metric_context(self):
        state = {
            "intent": {"target_metric": "payment_success_rate"},
            "evidence_brief": {"limitations": ["source_unbound"]},
            "validator_results": [],
        }
        output = {
            "status": "degraded",
            "explanation": "支付成功率暂时不可核验，付费金额仅作为相关缺口背景。",
            "repair_path": "补齐数据源后重跑。",
        }

        result = _sanitize_terminal_explanation(output, state, "degraded")

        self.assertIn("支付成功率", result["explanation"])

    def test_degraded_explanation_semantic_rejection_fails_after_one_node_call(self):
        from bi_agent.runtime.langgraph_workflow import _generate_degraded_explanation

        state = {
            "request": {"run_mode": "production"},
            "run_id": "degraded-semantic-retry",
            "intent": {
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "yesterday",
            },
            "evidence_brief": {"limitations": ["source_unbound"]},
            "validator_results": [],
            "verifier": {},
            "evidence": [],
            "draft_claims": [],
        }
        payloads = []

        def explain(_state, task, payload, **_kwargs):
            self.assertEqual(task, "degraded_explanation")
            payloads.append(payload)
            if len(payloads) == 1:
                return {
                    "explanation": "利润指标所需数据暂时不可用。",
                    "repair_path": "补齐数据源后重跑。",
                }
            return {
                "explanation": "付费金额所需数据暂时不可用。",
                "repair_path": "补齐数据源后重跑。",
            }

        with patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
            side_effect=explain,
        ):
            with self.assertRaisesRegex(WorkflowFailure, "target_metric_drift"):
                _generate_degraded_explanation(state)

        self.assertEqual(len(payloads), 1)

    def test_degraded_explanation_rejects_missing_business_fields(self):
        state = {
            "evidence_brief": {"limitations": ["source_unbound"]},
            "validator_results": [],
        }
        base = {
            "status": "degraded",
            "explanation": "付费金额所需业务证据暂时不可用。",
            "repair_path": "补齐业务证据后重跑。",
        }
        for field, reason in (
            ("explanation", "explanation_missing"),
            ("repair_path", "repair_path_missing"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                WorkflowFailure,
                reason,
            ):
                _sanitize_terminal_explanation({**base, field: ""}, state, "degraded")

    def test_degraded_explanation_rejects_non_string_or_untrimmed_fields(self):
        state = {
            "evidence_brief": {"limitations": ["source_unbound"]},
            "validator_results": [],
        }
        base = {
            "status": "degraded",
            "explanation": "付费金额所需业务证据暂时不可用。",
            "repair_path": "补齐业务证据后重跑。",
        }
        invalid = (
            ("explanation", {"text": "说明"}),
            ("repair_path", ["重跑"]),
            ("explanation", " 付费金额所需业务证据暂时不可用。 "),
        )
        for field, value in invalid:
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                WorkflowFailure,
                f"{field}_invalid",
            ):
                _sanitize_terminal_explanation(
                    {**base, field: value},
                    state,
                    "degraded",
                )

    def test_degraded_explanation_rejects_data_source_repair_for_business_limits(self):
        state = {
            "evidence_brief": {
                "limitations": ["below_materiality_floor", "weak_direction"]
            },
            "validator_results": [],
        }
        output = {
            "status": "degraded",
            "explanation": "变化幅度低于重要性阈值，方向一致性不足。",
            "repair_path": "检查数据源完整性和提取逻辑。",
        }

        with self.assertRaisesRegex(WorkflowFailure, "materiality_drift"):
            _sanitize_terminal_explanation(output, state, "degraded")

    def test_degraded_explanation_rejects_data_quality_drift_for_business_limits(self):
        state = {
            "evidence_brief": {
                "limitations": ["below_materiality_floor", "weak_direction"]
            },
            "validator_results": [],
        }
        output = {
            "status": "degraded",
            "explanation": "现有数据质量不足以支持该模式判定。",
            "repair_path": "延长观察周期。",
        }

        with self.assertRaisesRegex(WorkflowFailure, "materiality_drift"):
            _sanitize_terminal_explanation(output, state, "degraded")

    def test_degraded_explanation_keeps_status_local_and_omits_provider_extras(self):
        provider_output = {
            "status": "provider_selected_status",
            "explanation": "付费金额的已验证因素可以继续分析，支付成功率当前缺少独立观测。",
            "repair_path": "后续补充支付发起数据，可进一步下钻支付成功率。",
            "display_summary": "已保留可验证因素的分析结论。",
            "claims": [{"text": "支付成功率没有影响。"}],
        }
        client, completions = _provider_client_with_outputs((provider_output,))
        state = {
            "request": {},
            "intent": {"target_metric": "paid_amount"},
            "analysis_runtime_result": type(
                "DegradedResult",
                (),
                {
                    "typed_gaps": ({
                        "gap_type": "source_unbound",
                        "owner": "data_owner",
                        "requires_clarification": False,
                    },),
                },
            )(),
            "contract_gap_diagnostics": (),
            "evidence_brief": {"limitations": ["source_unbound"]},
            "validator_results": [],
            "llm_client": client,
            "llm_calls": [],
        }

        explanation = workflow_module._invoke_terminal_explanation(
            state,
            task="degraded_explanation",
            payload={},
            status="degraded",
        )

        self.assertEqual(explanation["status"], "degraded")
        self.assertNotIn("owner", explanation)
        self.assertNotIn("claims", explanation)
        self.assertEqual(completions.attempt_count, 1)

    def test_degraded_explanation_prompt_keeps_owner_out_of_business_output(self):
        spec = build_prompt("degraded_explanation", {"intent": {}})
        prompt = "\n".join(message["content"] for message in spec.messages)

        self.assertNotIn("status", spec.required_keys)
        self.assertNotIn("owner", spec.required_keys)
        self.assertIn("accountability remains in the internal gap audit", prompt)

    def test_unknown_typed_gap_owner_does_not_block_business_explanation(self):
        output = {
            "status": "blocked",
            "explanation": "当前数据在分析时点尚不可用，暂时不能发布业务结论。",
            "repair_path": "等待数据可用后重新运行本次分析。",
            "display_summary": "当前仅提供证据边界说明。",
        }
        client, completions = _provider_client_with_outputs((output,))
        state = {
            "request": {},
            "intent": {"target_metric": "paid_amount"},
            "analysis_runtime_result": type(
                "BlockedResult",
                (),
                {
                    "typed_gaps": ({
                        "gap_type": "source_unbound",
                        "owner": "unreviewed_owner",
                        "requires_clarification": True,
                    },),
                },
            )(),
            "contract_gap_diagnostics": (),
            "evidence_brief": {},
            "validator_results": [],
            "llm_client": client,
            "llm_calls": [],
        }

        explanation = workflow_module._invoke_terminal_explanation(
            state,
            task="blocked_explanation",
            payload={},
            status="blocked",
        )

        self.assertEqual(explanation["status"], "blocked")
        self.assertNotIn("owner", explanation)
        self.assertEqual(completions.attempt_count, 1)


    def test_blocked_explanation_invalid_provider_narrative_retries_three_times(self):
        for field, invalid_value in (
            ("explanation", "paid_amount evidence_ref"),
            ("repair_path", "Inspect evidence_ref before rerun."),
        ):
            with self.subTest(field=field):
                output = {
                    "status": "blocked",
                    "explanation": "当前数据在分析时点尚不可用，暂时不能发布业务结论。",
                    "repair_path": "等待数据可用后重新运行本次分析。",
                    "display_summary": "当前仅提供证据边界说明。",
                    field: invalid_value,
                }
                client, completions = _provider_client_with_outputs((output,))
                state = {
                    "request": {},
                    "intent": {"target_metric": "paid_amount"},
                    "analysis_runtime_result": type(
                        "BlockedResult",
                        (),
                        {
                            "typed_gaps": ({
                                "gap_type": "source_unbound",
                                "owner": "data_owner",
                                "requires_clarification": True,
                            },),
                        },
                    )(),
                    "contract_gap_diagnostics": (),
                    "evidence_brief": {},
                    "validator_results": [],
                    "llm_client": client,
                    "llm_calls": [],
                }

                with self.assertRaisesRegex(
                    WorkflowFailure,
                    f"llm_narrative_invalid:{field}",
                ):
                    workflow_module._invoke_terminal_explanation(
                        state,
                        task="blocked_explanation",
                        payload={},
                        status="blocked",
                    )

                self.assertEqual(completions.attempt_count, 3)
                self.assertEqual(state["llm_calls"][-1]["attempt_count"], 3)

    def test_semantic_audit_revision_routes_to_prose_repair_and_preserves_local_claims(self):
        fake = ScriptedLLMClient(
            {
                "semantic_audit": {
                    "audit_status": "needs_revision",
                    "extracted_claims": [],
                    "issues": [
                        {
                            "code": "unsupported_main_claim",
                            "severity": "error",
                            "description": "主要结论的原因表述过强。",
                        }
                    ],
                },
                "answer_repair": {
                    "answer_text": "已降低原因表述强度，并保留本地核验通过的事实。",
                }
            }
        )
        state = _required_claim_resolution_state()
        state.update(
            {
                "run_id": "semantic-repair-node",
                "llm_client": fake,
                "llm_calls": [],
                "answer_text": "当前因素已经证明了业务机制。",
                "validator_results": [],
            }
        )
        _reduce_evidence(state)
        state["draft_claims"] = workflow_module._authority_claims_from_evidence(state)
        original_claims = deepcopy(state["draft_claims"])

        workflow_module._semantic_audit(state)

        self.assertEqual(_route_after_semantic_audit(state), "repair")
        workflow_module._repair_answer(state)
        self.assertEqual(state["draft_claims"], original_claims)
        self.assertIn("降低原因表述强度", state["answer_text"])
        self.assertNotIn("degraded_explanation", fake.calls)

    def test_answer_repair_receives_business_semantic_review_without_runtime_reason(self):
        fake = ScriptedLLMClient(
            {
                "semantic_audit": {
                    "audit_status": "needs_revision",
                    "extracted_claims": [],
                    "issues": [
                        {
                            "code": "unsupported_main_claim",
                            "severity": "error",
                            "description": "答案声明超出证据。",
                        }
                    ],
                },
                "answer_repair": {
                    "answer_text": "已按业务证据边界修正答案声明。",
                    "display_summary": "业务回答已经修正。",
                },
            }
        )
        state = _required_claim_resolution_state()
        state.update(
            {
                "run_id": "semantic-review-projection",
                "llm_client": fake,
                "llm_calls": [],
                "answer_text": "当前因素已经证明了业务机制。",
                "validator_results": [],
            }
        )
        _reduce_evidence(state)
        state["draft_claims"] = workflow_module._authority_claims_from_evidence(state)

        workflow_module._semantic_audit(state)
        workflow_module._repair_answer(state)

        payload = _llm_input_payload(
            {"admin_audit": {"llm_calls": state["llm_calls"]}},
            "answer_repair",
        )
        self.assertEqual(
            set(payload),
            {"answerText", "businessContext", "displayReview"},
        )
        self.assertIn("主要结论超出", " ".join(payload["displayReview"]["findings"]))
        visible = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("retry_context", visible)
        self.assertNotIn("failure_reason", visible)
        self.assertNotIn("unsupported_main_claim", visible)

    def test_answer_repair_receives_business_verifier_review_without_internal_errors(self):
        fake = ScriptedLLMClient(
            {
                "answer_repair": {
                    "answer_text": "已按已验证数值修正业务回答。",
                    "display_summary": "业务回答已经修正。",
                }
            }
        )
        state = _required_claim_resolution_state()
        state.update(
            {
                "run_id": "verifier-review-projection",
                "llm_client": fake,
                "llm_calls": [],
                "answer_text": "当前答案含有一个待修正数字。",
                "validator_results": [],
            }
        )
        _reduce_evidence(state)
        state["draft_claims"] = workflow_module._authority_claims_from_evidence(state)
        state["authority_verified_claims"] = deepcopy(state["draft_claims"])
        original_claims = deepcopy(state["draft_claims"])
        state["verifier"] = {
            "status": "failed",
            "errors": [{"code": "number_mismatch", "claim_index": 0}],
        }
        state["retry_context"] = {
            "failed_node": "hard_verify_answer",
            "failure_type": "verifier",
            "failure_reason": "number_mismatch:claim_index=0",
        }

        workflow_module._repair_answer(state)

        self.assertEqual(state["draft_claims"], original_claims)
        payload = _llm_input_payload(
            {"admin_audit": {"llm_calls": state["llm_calls"]}},
            "answer_repair",
        )
        findings = " ".join(payload["displayReview"]["findings"])
        self.assertIn("已验证证据存在矛盾", findings)
        self.assertIn("主要结论超出", findings)
        visible = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("number_mismatch", visible)
        self.assertNotIn("hard_verify_answer", visible)
        self.assertNotIn("retry_context", visible)

    def test_final_summary_display_repair_ignores_cautious_evidence_gaps(self):
        state = {
            "intent": {"pattern_family": "custom_baseline"},
            "evidence": [],
            "evidence_brief": {"limitations": ["insufficient_comparable_periods"]},
        }
        summary = (
            "我对问题的理解是：你想判断活动是否影响付费金额变化。\n"
            "分析脉络：我检查了现有指标变化与活动窗口。\n"
            "关键发现：现有材料不足以形成稳定判断。\n"
            "最终结论：当前证据不足，不能确认活动带来了付费金额变化。\n"
            "需要注意：结论仅用于后续排查，仍需补充相关证据。"
        )

        self.assertFalse(_final_summary_needs_display_repair(summary, state))

    def test_quality_gate_rejects_answers_without_verified_claims(self):
        quality = evaluate_answer_quality(
            user_question="Q2 相比 Q1 付费金额为什么变了？",
            verified_claims=[],
            final_answer="最终结论：当前证据能把排查方向收敛到渠道贡献。",
            follow_up_questions=[
                "要看渠道贡献吗？",
                "要复核异常日期吗？",
                "要换成日均口径吗？",
            ],
        )

        self.assertFalse(quality["has_verified_claims"])
        self.assertFalse(quality["verified_claim_preserved"])
        self.assertIn("missing_verified_claim", quality["issues"])

    def test_quality_gate_accepts_paraphrased_insufficient_claim(self):
        quality = evaluate_answer_quality(
            user_question="昨天收入变化最大的是哪个维度？",
            verified_claims=[
                {
                    "text": "当前证据不足以支撑主业务结论；主要限制是数据字段缺失、可比周期不足。",
                    "numbers": {},
                    "claim_strength": "insufficient",
                }
            ],
            final_answer=(
                "最终结论：当前证据不足，无法确认收入变化最大的维度和因子；"
                "主要限制包括包名和玩法字段缺失、可比周期不足。"
                "当前证据能把排查方向收敛到数据补齐。"
            ),
            follow_up_questions=[
                "要先补哪些字段？",
                "要复核可比周期吗？",
                "要看已有维度覆盖吗？",
            ],
        )

        self.assertTrue(quality["verified_claim_preserved"])
        self.assertNotIn("missing_verified_claim", quality["issues"])

    def test_quality_gate_accepts_insufficient_claim_with_unsupported_conclusion_wording(self):
        quality = evaluate_answer_quality(
            user_question="昨天活动是否影响了付费金额？",
            verified_claims=[
                {
                    "text": "当前证据不足以支撑主业务结论；主要限制是变化幅度低于当前重要性阈值。",
                    "numbers": {},
                    "claim_strength": "insufficient",
                }
            ],
            final_answer=(
                "最终结论：根据现有证据，无法支持活动影响付费金额的结论；"
                "主要限制包括变化幅度低于当前重要性阈值。"
            ),
            follow_up_questions=[
                "要补活动记录吗？",
                "要扩大对比窗口吗？",
                "要看渠道分层吗？",
            ],
        )

        self.assertTrue(quality["verified_claim_preserved"])
        self.assertNotIn("missing_verified_claim", quality["issues"])

    def test_quality_gate_accepts_generic_evidence_strength_limitation(self):
        quality = evaluate_answer_quality(
            user_question="昨天收入变化最大的是哪个维度？",
            verified_claims=[
                {
                    "text": "当前证据不足以支撑主业务结论；主要限制是当前证据强度不足。",
                    "numbers": {},
                    "claim_strength": "insufficient",
                }
            ],
            final_answer=(
                "最终结论：基于当前证据，无法得出昨天收入变化的确定原因。"
                "现有证据强度不足以支撑主业务结论。"
            ),
            follow_up_questions=[
                "要先补哪些字段？",
                "要复核可比周期吗？",
                "要看已有维度覆盖吗？",
            ],
        )

        self.assertTrue(quality["verified_claim_preserved"])
        self.assertNotIn("missing_verified_claim", quality["issues"])

    def test_quality_gate_accepts_business_paraphrase_with_claim_numbers(self):
        quality = evaluate_answer_quality(
            user_question="Q2 相比 Q1 付费金额为什么变了？",
            verified_claims=[
                {
                    "text": "Q2 相比 Q1 的付费金额提升 20.0%，当前只支持窗口对比结论。",
                    "numbers": {"change_pct": 0.2},
                    "claim_strength": "observed",
                }
            ],
            final_answer=(
                "最终结论：从当前窗口对比看，付费金额比基线高 20.0%，"
                "这能作为观察结论使用，不能直接写成原因定论。"
            ),
            follow_up_questions=[
                "要看渠道贡献吗？",
                "要复核异常日期吗？",
                "要换成日均口径吗？",
            ],
        )

        self.assertTrue(quality["verified_claim_preserved"])
        self.assertNotIn("missing_verified_claim", quality["issues"])

    def test_final_audit_cannot_grant_missing_claim_preservation(self):
        state = {
            "request": {
                "run_mode": "production",
                "question": "Q2 相比 Q1 付费金额发生了什么变化？",
            },
            "answer_text": "最终结论：当前答案没有复述已验证数值。",
            "final_business_summary": "最终结论：当前答案没有复述已验证数值。",
            "authority_verified_claims": [
                {
                    "text": "Q2 相比 Q1 的付费金额提升 20.0%。",
                    "numbers": {"change_pct": 0.2},
                    "claim_strength": "observed",
                }
            ],
            "draft_claims": [],
            "verifier": {"errors": []},
            "evidence": [],
        }
        ready_audit = {
            "display_status": "ready",
            "hard_blockers": [],
            "repairable_warnings": [],
            "risk_flags": [],
            "retry_instruction": "",
            "business_audit_summary": "表达审阅通过。",
            "blocks_display": False,
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow._final_answer_audit",
            return_value=ready_audit,
        ):
            _answer_quality_gate(state)

        self.assertFalse(state["quality_gate"]["verified_claim_preserved"])
        self.assertIn("missing_verified_claim", state["quality_gate"]["issues"])


if __name__ == "__main__":
    unittest.main()
