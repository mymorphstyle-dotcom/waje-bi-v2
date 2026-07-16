import json
from dataclasses import asdict
from datetime import datetime

import pytest

from bi_agent.runtime.analysis_contracts import (
    AnalysisContract,
    analysis_contract_from_dict,
    analysis_contract_signature,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


def _registry() -> RuntimeContractRegistry:
    return RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)


def _current_business_intent_output(output: dict) -> dict:
    current = dict(output)
    current.setdefault("pattern_params", {})
    requirements = dict(current.get("analysis_requirements") or {})
    claim_intents = list(requirements.get("claim_intents") or ())
    requirements.setdefault(
        "claim_intent_roles",
        {claim_intent: "user_required" for claim_intent in claim_intents},
    )
    current["analysis_requirements"] = requirements
    return current


def _route_provider(route_output: dict):
    def invoke(state, node, payload, **kwargs):
        if node != "final_route_narrative":
            return route_output
        steps = payload["route_context"]["route_steps"]
        return {
            "route_summary": "先按已确认的业务范围核对数据，再汇总各项结论。",
            "sections": [
                {
                    "step_ref": step["step_ref"],
                    "route_step": "核对这一业务步骤对应的数据变化。",
                    "expected_evidence": "取得可核验的业务结果和边界说明。",
                }
                for step in steps
            ],
            "decision_summary": "已保留当前问题所需的分析路线。",
            "display_summary": "分析路线已确认，可以继续核验数据。",
        }

    return invoke


def _executed_market_authority(tmp_path, monkeypatch) -> dict:
    from tools.phase7 import run_live_conversation_system_test as system_test

    capabilities = (
        "data_quality_profile",
        "formula_decompose",
        "market_health_compare",
    )
    run_id = "run-track-b-market-authority"
    admin_audit = {
        "analysis_contract": AnalysisContract(
            analysis_contract_id=f"analysis:{run_id}:1",
            contract_version="1",
            question_families=("revenue_health_review",),
            target_metric_refs=("paid_amount",),
            claim_intents=(),
            scope={},
            business_timezone="Europe/London",
            as_of="2026-06-03T12:00:00+01:00",
            resolved_windows=(),
            metric_bindings=(),
            dimension_bindings=(),
            dataset_requirements=("market_dashboard",),
            capability_requirements=capabilities,
            permission_scope="analyst",
        ).to_dict(),
        "query_executions": [
            {
                "dataset_id": "market_dashboard",
                "result_ref": f"result:{capability}",
                "execution_status": "succeeded",
                "completeness_status": "complete",
                "analysis_readiness": "ready",
            }
            for capability in capabilities
        ],
        "capability_bindings": [
            {
                "capability_id": capability,
                "status": "ready",
                "result_refs": [f"result:{capability}"],
            }
            for capability in capabilities
        ],
    }
    monkeypatch.setattr(system_test, "ROOT", tmp_path)
    path = tmp_path / "artifacts" / "phase-7" / run_id / "answer_package.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"run_id": run_id, "admin_audit": admin_audit}),
        encoding="utf-8",
    )
    contract = admin_audit["analysis_contract"]
    signature = analysis_contract_signature(
        analysis_contract_from_dict(contract)
    )
    return system_test._runtime_audit_package(
        {
            "run_id": run_id,
            "artifact_path": str(path),
        },
        authority_resolver=lambda _run_id: {
            "run_id": run_id,
            "analysis_contract": {
                **contract,
                "contract_signature": signature,
            },
            "stored_contract_signature": signature,
        },
    )


def test_obligation_review_does_not_execute_from_legacy_market_bindings(
    tmp_path, monkeypatch
):
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    review = review_case_obligations(
        {
            "status": "completed",
            "answer_package": {"summary": "authority-backed terminal answer"},
            "accepted_graph": [
                "data_quality_profile",
                "formula_decompose",
                "market_health_compare",
            ],
            "scenario": {
                "question_family": "revenue_health_review",
                "target_metrics": ["paid_amount"],
                "expected_dataset_states": {"market_dashboard": "executable"},
                "allowed_claim_ceiling": "directional",
                "terminal_boundary": "verified_answer",
            },
            "runtime_authority": _executed_market_authority(tmp_path, monkeypatch),
        },
        _registry(),
    )

    assert "market_health_compare" in review["independent_capabilities"]
    assert "market_health_compare" in review["required_capabilities"]
    assert review["capability_outcomes"]["market_health_compare"] == "unobserved"
    assert "market_health_compare" in review["nonterminal_required_capabilities"]


def test_independent_capability_selects_its_dataset_authority_cell_only(
    tmp_path, monkeypatch
):
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    review = review_case_obligations(
        {
            "status": "completed",
            "answer_package": {"summary": "authority-backed terminal answer"},
            "accepted_graph": [
                "data_quality_profile",
                "formula_decompose",
                "market_health_compare",
            ],
            "scenario": {
                "question_family": "revenue_health_review",
                "target_metrics": ["paid_amount"],
                "expected_dataset_states": {"market_dashboard": "executable"},
                "allowed_claim_ceiling": "directional",
                "terminal_boundary": "verified_answer",
            },
            "runtime_authority": _executed_market_authority(tmp_path, monkeypatch),
        },
        _registry(),
        coverage_authority={
            "cells": {
                "market_health_compare:market_dashboard": {
                    "capability": "market_health_compare",
                    "datasets": ["market_dashboard"],
                    "question_families": ["revenue_health_review"],
                    "state": "executable",
                },
                "source_reconciliation:market_dashboard": {
                    "capability": "source_reconciliation",
                    "datasets": ["market_dashboard"],
                    "question_families": [],
                    "state": "contract_partial",
                },
                "market_health_compare:unrelated_family": {
                    "capability": "market_health_compare",
                    "datasets": ["market_dashboard"],
                    "question_families": ["paid_amount_change_explanation"],
                    "state": "contract_partial",
                },
            }
        },
    )

    assert review["expected_dataset_states"] == {"market_dashboard": "executable"}
    assert review["ambiguous_authority_dataset_roles"] == []
    assert review["hard_acceptance_passed"] is False


def test_unknown_diagnostic_is_rejected_without_erasing_family_obligations():
    from bi_agent.runtime.langgraph_workflow import reconcile_analysis_route

    route = {
        "analysis_requirements": {
            "target_metrics": ["paid_amount"],
            "diagnostic_tags": ["unknown_llm_diagnostic"],
        }
    }
    intent = {
        "question_family": "data_quality_or_evidence_review",
        "question_families": ["data_quality_or_evidence_review"],
        "target_metric": "paid_amount",
    }

    requested, output = reconcile_analysis_route(
        ("data_quality_profile",), route, intent, _registry()
    )

    assert {"metric_coverage_profile", "answer_verify"} <= set(requested)
    assert output["analysis_requirements"]["diagnostic_tags"] == []
    assert {
        (item["capability"], item["reason"])
        for item in output["obligation_resolution"]["mutations"]
    } >= {("unknown_llm_diagnostic", "unknown_diagnostic_rejected")}


@pytest.mark.parametrize(
    "diagnostic_tags",
    ["data_quality", [42], ["data_quality", "data_quality"], [""]],
)
def test_malformed_diagnostics_fail_route_contract(diagnostic_tags):
    from bi_agent.runtime import langgraph_workflow as workflow

    with pytest.raises(
        workflow.WorkflowFailure,
        match="analysis_route_contract_invalid:diagnostic_tags",
    ) as exc:
        workflow.reconcile_analysis_route(
            ("data_quality_profile",),
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "diagnostic_tags": diagnostic_tags,
                }
            },
            {
                "question_family": "data_quality_or_evidence_review",
                "question_families": ["data_quality_or_evidence_review"],
                "target_metric": "paid_amount",
            },
            _registry(),
        )

    assert exc.value.failure_type == "llm_contract"


def test_business_intent_carries_registry_validated_material_requirements(
    monkeypatch,
):
    from bi_agent.runtime import langgraph_workflow as workflow

    captured = {}

    def invoke(state, node, payload, **kwargs):
        captured.update(payload)
        return _current_business_intent_output({
            "question_family": "business_object_impact_review",
            "question_families": [
                "business_object_impact_review",
                "data_quality_or_evidence_review",
            ],
            "primary_question_family": "business_object_impact_review",
            "secondary_question_families": ["data_quality_or_evidence_review"],
            "target_metric": "paid_amount",
            "pattern_family": "custom_baseline",
            "scope": "full_sample",
            "time_window": "yesterday",
            "target_claim": "bounded impact review",
            "baseline_candidates": ["previous_day"],
            "status_message": "已绑定业务问题。",
            "answer_contract": {"direct_answer": True},
            "analysis_requirements": {
                "context_sources": ["external_event"],
                "claim_intents": ["candidate_mechanism"],
                "requested_dimensions": ["channel"],
                "requested_components": ["paid_users"],
            },
        })

    monkeypatch.setattr(workflow, "_invoke_llm", invoke)
    state = {"request": {"question": "arbitrary business question"}}

    workflow._understand_business_intent(state)

    assert state["intent"]["context_sources"] == ["external_event"]
    assert state["intent"]["claim_intents"] == ["candidate_mechanism"]
    assert state["intent"]["requested_dimensions"] == ["channel"]
    assert state["intent"]["requested_components"] == ["paid_users"]
    assert "external_event" in captured["allowed_context_source_ids"]
    assert "candidate_mechanism" in captured["allowed_claim_types"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("context_sources", ["unknown_dataset"]),
        ("context_sources", ["paid_order_success"]),
        ("claim_intents", ["candidate_mechanism", "candidate_mechanism"]),
        ("requested_dimensions", [42]),
        ("requested_components", "paid_users"),
    ],
)
def test_business_intent_material_requirements_fail_closed(field, value):
    from bi_agent.runtime import langgraph_workflow as workflow

    requirements = {
        "context_sources": [],
        "claim_intents": [],
        "requested_dimensions": [],
        "requested_components": [],
    }
    requirements[field] = value

    with pytest.raises(
        workflow.WorkflowFailure,
        match=f"business_intent_contract_invalid:analysis_requirements:{field}",
    ) as exc:
        workflow._validated_business_intent_requirements(
            requirements, _registry()
        )

    assert exc.value.failure_type == "llm_contract"


def test_business_intent_missing_context_family_axis_fails_after_one_node_call(
    monkeypatch,
):
    from bi_agent.runtime import langgraph_workflow as workflow

    payloads = []

    def invoke(state, node, payload, **kwargs):
        payloads.append(payload)
        return _current_business_intent_output({
            "question_family": "data_quality_or_evidence_review",
            "question_families": ["data_quality_or_evidence_review"],
            "primary_question_family": "data_quality_or_evidence_review",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
            "pattern_family": "custom_baseline",
            "scope": "full_sample",
            "time_window": "yesterday",
            "target_claim": "bounded context review",
            "baseline_candidates": [],
            "status_message": "已绑定业务问题。",
            "answer_contract": {"direct_answer": True},
            "analysis_requirements": {
                "context_sources": [],
                "claim_intents": ["contract_coverage_and_trust_boundary"],
                "requested_dimensions": ["gameplay"],
                "requested_components": [],
            },
        })

    monkeypatch.setattr(workflow, "_invoke_llm", invoke)
    state = {
        "run_id": "run-context-family-repair",
        "request": {"question": "arbitrary context question"},
        "checkpoint_events": [],
    }

    with pytest.raises(
        workflow.WorkflowFailure,
        match="context_family_axis_missing:dimension:gameplay:gameplay",
    ):
        workflow._retrying_node(
            "understand_business_intent", workflow._understand_business_intent
        )(state)

    assert len(payloads) == 1
    assert "node_retry_feedback" not in payloads[0]
    assert [event["status"] for event in state["checkpoint_events"]] == ["failed"]


def test_channel_dimension_does_not_create_unrelated_context_family_axis():
    from bi_agent.runtime import langgraph_workflow as workflow

    workflow._validate_context_family_axis(
        {
            "target_metric": "paid_amount",
            "question_families": ["data_quality_or_evidence_review"],
            "context_sources": [],
            "requested_dimensions": ["channel"],
        },
        _registry(),
    )


def test_business_context_source_allowlist_excludes_metric_only_datasets():
    registry = _registry()

    assert set(registry.context_source_ids) == {
        "gameplay",
        "gameplay_channel",
        "external_event",
        "internal_operation_event",
    }
    assert "paid_order_success" not in registry.context_source_ids


def test_context_family_compatibility_uses_exact_capability_dataset_allowlist():
    from bi_agent.runtime import langgraph_workflow as workflow

    registry = _registry()
    assert workflow._question_family_supports_context_dataset(
        "business_object_impact_review", "external_event", registry
    )
    assert not workflow._question_family_supports_context_dataset(
        "pattern_explanation", "gameplay", registry
    )
    assert not workflow._question_family_supports_context_dataset(
        "anomaly_or_black_swan_review", "gameplay", registry
    )
    assert registry.capability_inputs("event_evidence")[
        "allowed_context_datasets"
    ] == ["external_event", "internal_operation_event"]
    assert "gameplay" not in registry.capability_inputs("event_evidence")[
        "allowed_context_datasets"
    ]
    assert "external_event" not in registry.capability_inputs(
        "gameplay_activity_context"
    )["allowed_datasets"]


def test_context_family_axis_discovers_new_reviewed_family_from_registry():
    from bi_agent.runtime import langgraph_workflow as workflow

    class RegistryWithReviewedFamily:
        def __init__(self, base):
            self._base = base

        @property
        def question_family_ids(self):
            return (*self._base.question_family_ids, "reviewed_event_impact")

        def question_family_obligation(self, question_family):
            if question_family == "reviewed_event_impact":
                return {
                    "required_capabilities": [],
                    "independent_capabilities": ["event_evidence"],
                    "conditional_rules": [],
                }
            return self._base.question_family_obligation(question_family)

        def __getattr__(self, name):
            return getattr(self._base, name)

    registry = RegistryWithReviewedFamily(_registry())

    workflow._validate_context_family_axis(
        {
            "target_metric": "paid_amount",
            "question_families": ["reviewed_event_impact"],
            "context_sources": ["external_event"],
            "requested_dimensions": [],
        },
        registry,
    )


def test_business_intent_payload_exposes_registry_context_family_compatibility():
    from bi_agent.runtime import langgraph_workflow as workflow
    from bi_agent.runtime.llm_prompts import build_prompt

    payload = workflow._business_intent_payload(
        {
            "question": "检查收入变化及相关业务背景。",
            "run_mode": "production",
            "analysis_context": {"target_date": "2026-06-02"},
        }
    )

    assert payload["context_source_question_family_compatibility"] == {
        "external_event": [
            "anomaly_or_black_swan_review",
            "business_object_impact_review",
            "pattern_explanation",
        ],
        "gameplay": [
            "business_object_impact_review",
            "segment_or_factor_attribution",
        ],
        "gameplay_channel": [
            "business_object_impact_review",
            "segment_or_factor_attribution",
        ],
        "internal_operation_event": [
            "anomaly_or_black_swan_review",
            "business_object_impact_review",
            "pattern_explanation",
        ],
    }
    assert payload["dimension_question_family_compatibility"]["paid_amount"][
        "gameplay"
    ] == [
        "business_object_impact_review",
        "segment_or_factor_attribution",
    ]
    assert (
        "gameplay"
        not in payload["dimension_question_family_compatibility"][
            "player_bet_amount"
        ]
    )
    prompt_text = "\n".join(
        message["content"] for message in build_prompt("business_intent", payload).messages
    )
    assert "include at least one question family listed for that source" in prompt_text
    assert "add a compatible secondary family" in prompt_text


class _SyntheticContextRegistry:
    def __init__(
        self,
        base,
        *,
        metric_sources=None,
        dimension_sources=None,
        empty_family_capabilities=False,
    ):
        self._base = base
        self._metric_sources = metric_sources or {}
        self._dimension_sources = dimension_sources or {}
        self._empty_family_capabilities = empty_family_capabilities

    @property
    def dimension_ids(self):
        return tuple(
            dict.fromkeys((*self._base.dimension_ids, *self._dimension_sources))
        )

    def metric_sources(self, metric_id):
        if metric_id in self._metric_sources:
            return self._metric_sources[metric_id]
        return self._base.metric_sources(metric_id)

    def dimension_sources(self, dimension_id):
        if dimension_id in self._dimension_sources:
            return self._dimension_sources[dimension_id]
        return self._base.dimension_sources(dimension_id)

    def question_family_obligation(self, question_family):
        if self._empty_family_capabilities:
            return {
                "required_capabilities": [],
                "independent_capabilities": [],
                "conditional_rules": [],
            }
        return self._base.question_family_obligation(question_family)

    def __getattr__(self, name):
        return getattr(self._base, name)


def test_context_family_axis_checks_explicit_context_source_even_when_dual_role():
    from bi_agent.runtime import langgraph_workflow as workflow

    registry = _SyntheticContextRegistry(
        _registry(), metric_sources={"paid_amount": ["gameplay"]}
    )

    with pytest.raises(
        workflow.WorkflowFailure,
        match="context_family_axis_missing:gameplay",
    ):
        workflow._validate_context_family_axis(
            {
                "target_metric": "paid_amount",
                "question_families": ["revenue_health_review"],
                "context_sources": ["gameplay"],
                "requested_dimensions": [],
            },
            registry,
        )


def test_metric_native_context_backed_dimension_needs_no_unrelated_family():
    from bi_agent.runtime import langgraph_workflow as workflow

    workflow._validate_context_family_axis(
        {
            "target_metric": "player_bet_amount",
            "question_families": ["revenue_health_review"],
            "context_sources": [],
            "requested_dimensions": ["gameplay"],
        },
        _registry(),
    )


def test_context_backed_multi_source_dimension_uses_exposed_family_union():
    from bi_agent.runtime import langgraph_workflow as workflow

    registry = _SyntheticContextRegistry(
        _registry(),
        dimension_sources={
            "composite_context": ["gameplay", "external_event"]
        },
    )

    workflow._validate_context_family_axis(
        {
            "target_metric": "paid_amount",
            "question_families": ["anomaly_or_black_swan_review"],
            "context_sources": [],
            "requested_dimensions": ["composite_context"],
        },
        registry,
    )


def test_context_family_axis_rejects_empty_registry_compatibility():
    from bi_agent.runtime import langgraph_workflow as workflow

    registry = _SyntheticContextRegistry(
        _registry(), empty_family_capabilities=True
    )

    with pytest.raises(
        workflow.WorkflowFailure,
        match="context_family_axis_unmapped:gameplay",
    ):
        workflow._validate_context_family_axis(
            {
                "target_metric": "paid_amount",
                "question_families": ["business_object_impact_review"],
                "context_sources": ["gameplay"],
                "requested_dimensions": [],
            },
            registry,
        )


def _context_family_provider_state(outputs):
    from bi_agent.runtime.llm_client import OpenAICompatibleLLMClient

    class SequencedCompletions:
        attempt_count = 0

        def create(self, **kwargs):
            output = outputs[min(self.attempt_count, len(outputs) - 1)]
            self.attempt_count += 1
            message = type(
                "Message",
                (),
                {"content": json.dumps(output, ensure_ascii=False)},
            )()
            choice = type("Choice", (), {"message": message})()
            return type(
                "Response",
                (),
                {
                    "id": f"context-family-{self.attempt_count}",
                    "choices": [choice],
                    "usage": None,
                },
            )()

    completions = SequencedCompletions()
    client = OpenAICompatibleLLMClient(
        provider="openai_compatible",
        model="context-family-model",
        api_key="test-key",
    )
    client._client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": completions})()},
    )()
    return {
        "request": {
            "question": "检查收入变化及相关业务背景。",
            "run_mode": "production",
            "analysis_context": {"target_date": "2026-06-02"},
        },
        "llm_client": client,
        "llm_calls": [],
        "checkpoint_events": [],
    }, completions


@pytest.mark.parametrize(
    ("requirements", "valid_secondary_family"),
    (
        (
            {
                "context_sources": ["gameplay"],
                "claim_intents": ["comparative_change"],
                "requested_dimensions": [],
                "requested_components": [],
            },
            "business_object_impact_review",
        ),
        (
            {
                "context_sources": [],
                "claim_intents": ["segment_contribution_or_mix_shift"],
                "requested_dimensions": ["gameplay"],
                "requested_components": [],
            },
            "segment_or_factor_attribution",
        ),
    ),
)
def test_real_provider_retries_context_family_incoherence_inside_shared_client(
    requirements,
    valid_secondary_family,
):
    from bi_agent.runtime import langgraph_workflow as workflow

    invalid = _current_business_intent_output({
        "question_family": "revenue_health_review",
        "question_families": ["revenue_health_review"],
        "primary_question_family": "revenue_health_review",
        "secondary_question_families": [],
        "target_metric": "paid_amount",
        "pattern_family": "rolling",
        "pattern_params": {},
        "scope": "full_sample",
        "time_window": "2026-06-02",
        "target_claim": "检查付费金额经营表现及相关背景",
        "baseline_candidates": [],
        "analysis_requirements": requirements,
        "status_message": "已完成意图识别。",
        "display_summary": "已绑定业务问题。",
    })
    valid = {
        **invalid,
        "question_families": [
            "revenue_health_review",
            valid_secondary_family,
        ],
        "secondary_question_families": [valid_secondary_family],
    }
    outputs = [invalid, invalid, valid]
    state, completions = _context_family_provider_state(outputs)

    workflow._understand_business_intent(state)

    assert completions.attempt_count == 3
    assert valid_secondary_family in state["intent"]["question_families"]
    assert state["llm_calls"][-1]["attempt_count"] == 3


def test_real_provider_context_family_exhaustion_keeps_safe_failed_audit():
    from bi_agent.runtime import langgraph_workflow as workflow

    invalid = _current_business_intent_output({
        "question_family": "revenue_health_review",
        "question_families": ["revenue_health_review"],
        "primary_question_family": "revenue_health_review",
        "secondary_question_families": [],
        "target_metric": "paid_amount",
        "pattern_family": "rolling",
        "pattern_params": {},
        "scope": "full_sample",
        "time_window": "2026-06-02",
        "target_claim": "检查付费金额经营表现及相关背景",
        "baseline_candidates": [],
        "analysis_requirements": {
            "context_sources": ["gameplay"],
            "claim_intents": ["comparative_change"],
            "requested_dimensions": [],
            "requested_components": [],
        },
        "status_message": "已完成意图识别。",
        "display_summary": "已绑定业务问题。",
    })
    state, completions = _context_family_provider_state([invalid])

    with pytest.raises(
        workflow.WorkflowFailure,
        match="context_family_axis_missing:gameplay",
    ):
        workflow._understand_business_intent(state)

    assert completions.attempt_count == 3
    audit = state["llm_calls"][-1]
    assert audit["status"] == "failed"
    assert audit["attempt_count"] == 3
    assert len(audit["attempt_failures"]) == 3
    assert audit["failure_code"] == "context_family_axis_missing:gameplay"
    assert "raw_response_content" not in audit


def test_business_intent_provider_validator_uses_injected_registry(monkeypatch):
    from bi_agent.runtime import langgraph_workflow as workflow

    registry = _registry()

    def fail_registry_reload(*args, **kwargs):
        raise RuntimeError("unexpected_registry_reload")

    monkeypatch.setattr(
        workflow.RuntimeContractRegistry,
        "from_path",
        fail_registry_reload,
    )
    payload = workflow._business_intent_payload(
        {"question": "检查付费金额及相关业务背景。"},
        registry=registry,
    )
    assert payload["allowed_target_metric_ids"] == registry.metric_ids

    workflow._validate_business_intent_provider_output(
        _current_business_intent_output({
            "question_family": "revenue_health_review",
            "question_families": [
                "revenue_health_review",
                "business_object_impact_review",
            ],
            "primary_question_family": "revenue_health_review",
            "secondary_question_families": [
                "business_object_impact_review"
            ],
            "target_metric": "paid_amount",
            "pattern_family": "rolling",
            "pattern_params": {},
            "scope": "full_sample",
            "time_window": "2026-06-02",
            "target_claim": "检查付费金额及相关业务背景",
            "analysis_requirements": {
                "context_sources": ["gameplay"],
                "claim_intents": ["comparative_change"],
                "requested_dimensions": [],
                "requested_components": [],
            },
        }),
        {"run_mode": "production"},
        registry,
    )


def test_context_family_axis_rejects_incompatible_reviewed_family():
    from bi_agent.runtime import langgraph_workflow as workflow

    with pytest.raises(
        workflow.WorkflowFailure,
        match="context_family_axis_missing:gameplay",
    ):
        workflow._validate_context_family_axis(
            {
                "target_metric": "paid_amount",
                "question_families": ["pattern_explanation"],
                "context_sources": ["gameplay"],
                "requested_dimensions": [],
            },
            _registry(),
        )


def test_metric_only_dataset_never_establishes_context_family_compatibility():
    from bi_agent.runtime import langgraph_workflow as workflow

    registry = _registry()
    assert "business_context" not in registry.dataset("paid_order_success")[
        "intent_roles"
    ]
    assert not workflow._question_family_supports_context_dataset(
        "business_object_impact_review", "paid_order_success", registry
    )


def test_business_intent_context_family_axis_fails_closed_after_one_call(monkeypatch):
    from bi_agent.runtime import langgraph_workflow as workflow

    def invoke(state, node, payload, **kwargs):
        return _current_business_intent_output({
            "question_family": "data_quality_or_evidence_review",
            "question_families": ["data_quality_or_evidence_review"],
            "target_metric": "paid_amount",
            "pattern_family": "custom_baseline",
            "scope": "full_sample",
            "time_window": "yesterday",
            "target_claim": "bounded context review",
            "baseline_candidates": [],
            "status_message": "已绑定业务问题。",
            "answer_contract": {"direct_answer": True},
            "analysis_requirements": {
                "context_sources": ["gameplay"],
                "claim_intents": [],
                "requested_dimensions": [],
                "requested_components": [],
            },
        })

    monkeypatch.setattr(workflow, "_invoke_llm", invoke)
    state = {
        "run_id": "run-context-family-failed",
        "request": {"question": "arbitrary context question"},
        "checkpoint_events": [],
    }

    with pytest.raises(
        workflow.WorkflowFailure,
        match="context_family_axis_missing:gameplay",
    ):
        workflow._retrying_node(
            "understand_business_intent", workflow._understand_business_intent
        )(state)

    assert [event["status"] for event in state["checkpoint_events"]] == ["failed"]


def test_failed_business_intent_returns_one_llm_audit_and_failed_checkpoint():
    from types import SimpleNamespace

    from bi_agent.runtime import langgraph_workflow as workflow

    families = [
        "pattern_explanation",
        "anomaly_or_black_swan_review",
        "data_quality_or_evidence_review",
    ]

    class ContractFailingClient:
        def invoke_json(self, *, task, prompt_version, messages, required_keys):
            family = families.pop(0)
            output = _current_business_intent_output({
                "question_family": family,
                "question_families": [family],
                "primary_question_family": family,
                "secondary_question_families": [],
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "scope": "full_sample",
                "time_window": "yesterday",
                "target_claim": "玩法活动与付费变化的边界判断",
                "baseline_candidates": [],
                "status_message": "已识别业务问题。",
                "answer_contract": {"direct_answer": True},
                "analysis_requirements": {
                    "context_sources": ["gameplay"],
                    "claim_intents": ["comparative_change"],
                    "requested_dimensions": [],
                    "requested_components": [],
                },
            })
            attempt = 3 - len(families)
            return SimpleNamespace(
                output=output,
                audit={
                    "task": task,
                    "provider": "contract-test-provider",
                    "model": "contract-test-model",
                    "prompt_version": prompt_version,
                    "response_id": f"response-{attempt}",
                    "structured_output": output,
                    "raw_response_content": json.dumps(output, ensure_ascii=False),
                },
            )

    result = workflow.run_pattern_workflow(
        {
            "run_id": "run-failed-intent-audits",
            "run_mode": "production",
            "question": "昨天玩法活跃和付费变化能对上吗？",
            "llm_client": ContractFailingClient(),
        }
    )

    assert result.status == "failed"
    assert result.failure_reason == "context_family_axis_missing:gameplay"
    assert [
        call["structured_output"]["question_family"]
        for call in result.llm_calls
    ] == ["pattern_explanation"]
    assert [event["attempt"] for event in result.checkpoint_events] == [1]
    assert [event["status"] for event in result.checkpoint_events] == ["failed"]
    assert {
        event["reason"] for event in result.checkpoint_events
    } == {"context_family_axis_missing:gameplay"}


def test_successful_workflow_result_returns_llm_audits(monkeypatch):
    from bi_agent.runtime import langgraph_workflow as workflow

    audit = {
        "task": "business_intent",
        "provider": "contract-test-provider",
        "model": "contract-test-model",
        "prompt_version": "contract-test-v1",
        "response_id": "response-success",
        "structured_output": {
            "question_family": "business_object_impact_review"
        },
        "raw_response_content": json.dumps(
            {"question_family": "business_object_impact_review"},
            ensure_ascii=False,
        ),
    }

    class CompletedGraph:
        def invoke(self, state, config):
            state["llm_calls"].append(audit)
            return {
                **state,
                "workflow_status": "draft",
                "answer_package": {"status": "draft"},
                "artifact_path": "artifacts/phase-7/run-success-audits.json",
            }

    monkeypatch.setattr(workflow, "build_pattern_graph", CompletedGraph)
    result = workflow.run_pattern_workflow(
        {
            "run_id": "run-success-audits",
            "llm_client": object(),
        }
    )

    assert result.status == "draft"
    assert result.llm_calls == (audit,)


def test_route_design_metric_only_context_fails_after_one_node_call(
    monkeypatch,
):
    from bi_agent.runtime import langgraph_workflow as workflow

    payloads = []

    def invoke(state, node, payload, **kwargs):
        payloads.append(payload)
        return {
            "requested_nodes": ["gameplay_activity_context"],
            "analysis_requirements": {
                "target_metrics": ["paid_amount"],
                "context_sources": ["market_dashboard"],
            },
        }

    monkeypatch.setattr(workflow, "_invoke_llm", invoke)
    state = {
        "run_id": "run-route-context-repair",
        "intent": {
            "question_family": "business_object_impact_review",
            "question_families": ["business_object_impact_review"],
            "primary_question_family": "business_object_impact_review",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
            "context_sources": ["gameplay"],
            "requested_dimensions": ["gameplay"],
        },
        "confirmed_understanding": {},
        "request": {},
        "checkpoint_events": [],
    }
    with pytest.raises(
        workflow.WorkflowFailure,
        match="analysis_route_contract_invalid:analysis_requirements:context_sources",
    ):
        workflow._retrying_node(
            "design_analysis_route", workflow._design_analysis_route
        )(state)

    assert payloads[0]["allowed_context_source_ids"]
    assert len(payloads) == 1
    assert "node_retry_feedback" not in payloads[0]
    assert [event["status"] for event in state["checkpoint_events"]] == ["failed"]

    with pytest.raises(
        workflow.WorkflowFailure,
        match="analysis_route_contract_invalid:analysis_requirements:baselines",
    ):
        workflow._validate_route_analysis_requirements(
            {"analysis_requirements": {"baselines": "previous_day"}},
            _registry(),
        )
def test_route_baseline_proposals_are_bounded_by_the_reviewed_runtime_vocabulary(
    monkeypatch,
):
    from bi_agent.runtime import langgraph_workflow as workflow
    from bi_agent.runtime.window_resolver import CURRENT_DATA_BASELINES

    payloads = []

    def invoke(state, node, payload, **kwargs):
        payloads.append(payload)
        return {
            "requested_nodes": ["data_quality_profile"],
            "analysis_requirements": {
                "target_metrics": ["paid_amount"],
                "baselines": ["unreviewed_baseline_alias"],
            },
        }

    monkeypatch.setattr(workflow, "_invoke_llm", invoke)
    state = {
        "run_id": "run-route-baseline-vocabulary-repair",
        "intent": {
            "question_family": "data_quality_or_evidence_review",
            "question_families": ["data_quality_or_evidence_review"],
            "primary_question_family": "data_quality_or_evidence_review",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
        },
        "confirmed_understanding": {},
        "request": {},
        "checkpoint_events": [],
    }

    with pytest.raises(
        workflow.WorkflowFailure,
        match="analysis_route_contract_invalid:analysis_requirements:baselines",
    ):
        workflow._retrying_node(
            "design_analysis_route", workflow._design_analysis_route
        )(state)

    allowed = list(CURRENT_DATA_BASELINES)
    assert [payload["allowed_baseline_ids"] for payload in payloads] == [allowed]
    assert "node_retry_feedback" not in payloads[0]
    assert [event["status"] for event in state["checkpoint_events"]] == ["failed"]

    with pytest.raises(
        workflow.WorkflowFailure,
        match="analysis_route_contract_invalid:analysis_requirements:baselines",
    ):
        workflow._validate_route_analysis_requirements(
            {
                "analysis_requirements": {
                    "baselines": ["unreviewed_baseline_alias"]
                }
            },
            _registry(),
        )


def test_route_requirements_keep_metric_dataset_outside_context_sources():
    from bi_agent.runtime import langgraph_workflow as workflow

    workflow._validate_route_analysis_requirements(
        {
            "analysis_requirements": {
                "target_metrics": ["paid_amount"],
                "context_sources": [],
                "dataset_requirements": ["paid_order_success"],
            }
        },
        _registry(),
    )

    with pytest.raises(
        workflow.WorkflowFailure,
        match="analysis_route_contract_invalid:analysis_requirements:context_sources",
    ):
        workflow._validate_route_analysis_requirements(
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "context_sources": ["paid_order_success"],
                    "dataset_requirements": [],
                }
            },
            _registry(),
        )


def test_initial_route_preserves_all_authoritative_material_axes_in_contract(
    monkeypatch,
):
    from bi_agent.runtime import langgraph_workflow as workflow
    from bi_agent.runtime.analysis_contract_compiler import compile_analysis_contract
    from bi_agent.runtime.dataset_catalog import DatasetCatalog

    monkeypatch.setattr(
        workflow,
        "_invoke_llm",
        _route_provider({
            "requested_nodes": ["data_quality_profile"],
            "analysis_requirements": {
                "target_metrics": ["paid_amount"],
                "requested_components": [],
                "requested_dimensions": [],
                "baselines": [],
                "context_sources": [],
                "claim_intents": [],
                "scope": "full_sample",
            },
        }),
    )
    state = {
        "run_id": "run-route-material-closure",
        "intent": {
            "question_family": "data_quality_or_evidence_review",
            "question_families": ["data_quality_or_evidence_review"],
            "primary_question_family": "data_quality_or_evidence_review",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
            "requested_components": ["active_users"],
            "requested_dimensions": ["channel"],
            "baseline_candidates": ["previous_day"],
            "context_sources": ["external_event"],
            "claim_intents": ["contract_coverage_and_trust_boundary"],
            "scope": "full_sample",
        },
        "confirmed_understanding": {},
        "request": {},
    }

    workflow._design_analysis_route(state)

    requirements = state["analysis_route"]["analysis_requirements"]
    assert requirements["target_metrics"] == ["paid_amount"]
    assert requirements["requested_components"] == ["active_users"]
    assert requirements["requested_dimensions"] == ["channel"]
    assert requirements["baselines"] == ["previous_day"]
    assert requirements["context_sources"] == ["external_event"]
    assert requirements["claim_intents"] == [
        "contract_coverage_and_trust_boundary"
    ]

    outcome = compile_analysis_contract(
        run_id=state["run_id"],
        proposal={
            **requirements,
            "question_families": state["intent"]["question_families"],
        },
        accepted_capabilities=state["analysis_route"]["requested_nodes"],
        catalog=DatasetCatalog(()),
        registry=_registry(),
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        permission_scope="analyst",
    )
    contract = outcome.analysis_contract
    assert {"paid_amount", "active_users"} <= set(
        contract.scope["requested_metric_ids"]
    )
    assert "channel" in contract.scope["requested_dimension_ids"]
    assert contract.claim_intents == ("contract_coverage_and_trust_boundary",)
    assert any(
        gap.diagnostic_context.get("item_kind") == "dimension"
        and gap.diagnostic_context.get("item_id") == "channel"
        for gap in contract.contract_gaps
    )


def test_resume_route_merges_authoritative_material_before_validation():
    from bi_agent.runtime import langgraph_workflow as workflow

    state = {
        "run_id": "run-resume-route-material",
        "intent": {
            "question_family": "data_quality_or_evidence_review",
            "question_families": ["data_quality_or_evidence_review"],
            "primary_question_family": "data_quality_or_evidence_review",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
        },
        "confirmed_understanding": {},
        "request": {
            "clarification_resume_context": {
                "accepted_graph": ["data_quality_profile"],
                "analysis_route": {
                    "requested_nodes": ["data_quality_profile"],
                    "analysis_requirements": {
                        "target_metrics": ["paid_amount"],
                        "requested_components": ["active_users"],
                        "requested_dimensions": ["channel"],
                        "baselines": ["previous_day"],
                        "context_sources": ["external_event"],
                        "claim_intents": [
                            "contract_coverage_and_trust_boundary"
                        ],
                        "scope": "full_sample",
                    },
                },
                "analysis_contract": {
                    "question_families": ["data_quality_or_evidence_review"]
                },
                "material_slots": {
                    "target_metrics": ["paid_amount"],
                    "requested_components": ["active_users"],
                    "requested_dimensions": ["channel"],
                    "baselines": ["previous_day"],
                    "context_sources": ["external_event"],
                    "claim_intents": ["contract_coverage_and_trust_boundary"],
                    "scope": "full_sample",
                },
            }
        },
    }

    workflow._design_analysis_route(state)

    requirements = state["analysis_route"]["analysis_requirements"]
    assert requirements["requested_components"] == ["active_users"]
    assert requirements["requested_dimensions"] == ["channel"]
    assert requirements["baselines"] == ["previous_day"]
    assert requirements["context_sources"] == ["external_event"]
    assert requirements["claim_intents"] == [
        "contract_coverage_and_trust_boundary"
    ]


@pytest.mark.parametrize(
    "axis,prior_values,persisted_values",
    [
        ("target_metrics", ["active_users"], ["paid_amount"]),
        ("requested_components", ["paid_users"], ["active_users"]),
        ("requested_dimensions", ["channel"], ["gameplay"]),
        ("baselines", ["rolling_7_day_baseline"], ["previous_day"]),
        ("context_sources", ["external_event"], ["gameplay"]),
        (
            "claim_intents",
            ["comparative_change"],
            ["contract_coverage_and_trust_boundary"],
        ),
    ],
)
def test_resume_route_rejects_material_axis_drift(
    axis, prior_values, persisted_values
):
    from bi_agent.runtime import langgraph_workflow as workflow

    prior_requirements = {"target_metrics": ["paid_amount"]}
    prior_requirements[axis] = prior_values
    material_slots = {"target_metrics": ["paid_amount"]}
    material_slots[axis] = persisted_values
    state = {
        "run_id": "run-resume-route-material-drift",
        "intent": {
            "question_family": "business_object_impact_review",
            "question_families": ["business_object_impact_review"],
            "primary_question_family": "business_object_impact_review",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
        },
        "confirmed_understanding": {},
        "request": {
            "clarification_resume_context": {
                "accepted_graph": ["gameplay_activity_context"],
                "analysis_route": {
                    "requested_nodes": ["gameplay_activity_context"],
                    "analysis_requirements": prior_requirements,
                },
                "analysis_contract": {
                    "question_families": ["business_object_impact_review"]
                },
                "material_slots": material_slots,
            }
        },
    }

    with pytest.raises(
        workflow.WorkflowFailure,
        match=f"clarification_resume_material_slots_conflict:{axis}",
    ) as exc:
        workflow._design_analysis_route(state)

    assert exc.value.failure_type == "contract"


@pytest.mark.parametrize(
    "axis,prior_values",
    [
        ("target_metrics", ["paid_amount"]),
        ("requested_components", ["active_users"]),
        ("requested_dimensions", ["channel"]),
        ("baselines", ["previous_day"]),
        ("context_sources", ["external_event"]),
        ("claim_intents", ["contract_coverage_and_trust_boundary"]),
    ],
)
def test_resume_route_rejects_material_axis_missing_from_authority(
    axis, prior_values
):
    from bi_agent.runtime import langgraph_workflow as workflow

    prior_requirements = {"target_metrics": ["paid_amount"]}
    prior_requirements[axis] = prior_values
    material_slots = (
        {} if axis == "target_metrics" else {"target_metrics": ["paid_amount"]}
    )
    state = {
        "run_id": "run-resume-route-material-missing",
        "intent": {
            "question_family": "business_object_impact_review",
            "question_families": ["business_object_impact_review"],
            "primary_question_family": "business_object_impact_review",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
        },
        "confirmed_understanding": {},
        "request": {
            "clarification_resume_context": {
                "accepted_graph": ["gameplay_activity_context"],
                "analysis_route": {
                    "requested_nodes": ["gameplay_activity_context"],
                    "analysis_requirements": prior_requirements,
                },
                "analysis_contract": {
                    "question_families": ["business_object_impact_review"]
                },
                "material_slots": material_slots,
            }
        },
    }

    with pytest.raises(
        workflow.WorkflowFailure,
        match=f"clarification_resume_material_slots_conflict:{axis}",
    ) as exc:
        workflow._design_analysis_route(state)

    assert exc.value.failure_type == "contract"


@pytest.mark.parametrize(
    "axis",
    [
        "target_metrics",
        "requested_components",
        "requested_dimensions",
        "baselines",
        "context_sources",
        "claim_intents",
    ],
)
def test_strict_resume_merge_preserves_explicit_empty_authority(axis):
    from bi_agent.runtime import langgraph_workflow as workflow

    material_slots = {
        "target_metrics": [],
        "requested_components": [],
        "requested_dimensions": [],
        "baselines": [],
        "context_sources": [],
        "claim_intents": [],
    }
    merged, conflicts = workflow._merge_confirmed_material_requirements(
        {"analysis_requirements": {}},
        {
            "intent": {},
            "request": {
                "clarification_resume_context": {
                    "material_slots": material_slots
                }
            },
        },
        strict_resume_authority=True,
    )

    assert conflicts == ()
    assert axis in merged["analysis_requirements"]
    assert merged["analysis_requirements"][axis] == []


@pytest.mark.parametrize(
    "axis",
    [
        "target_metrics",
        "requested_components",
        "requested_dimensions",
        "baselines",
        "context_sources",
        "claim_intents",
    ],
)
def test_strict_resume_merge_does_not_treat_missing_authority_as_empty(axis):
    from bi_agent.runtime import langgraph_workflow as workflow

    material_slots = {
        "target_metrics": [],
        "requested_components": [],
        "requested_dimensions": [],
        "baselines": [],
        "context_sources": [],
        "claim_intents": [],
    }
    material_slots.pop(axis)
    _, conflicts = workflow._merge_confirmed_material_requirements(
        {"analysis_requirements": {axis: []}},
        {
            "intent": {},
            "request": {
                "clarification_resume_context": {
                    "material_slots": material_slots
                }
            },
        },
        strict_resume_authority=True,
    )

    assert conflicts == (axis,)


@pytest.mark.parametrize(
    "axis,proposed",
    [
        ("target_metrics", ["paid_amount"]),
        ("requested_components", ["paid_users"]),
        ("requested_dimensions", ["channel"]),
        ("baselines", ["previous_day"]),
        ("context_sources", ["gameplay"]),
        ("claim_intents", ["comparative_change"]),
    ],
)
def test_strict_resume_merge_rejects_nonempty_proposal_against_empty_authority(
    axis, proposed
):
    from bi_agent.runtime import langgraph_workflow as workflow

    material_slots = {
        "target_metrics": [],
        "requested_components": [],
        "requested_dimensions": [],
        "baselines": [],
        "context_sources": [],
        "claim_intents": [],
    }
    _, conflicts = workflow._merge_confirmed_material_requirements(
        {"analysis_requirements": {axis: proposed}},
        {
            "intent": {},
            "request": {
                "clarification_resume_context": {
                    "material_slots": material_slots
                }
            },
        },
        strict_resume_authority=True,
    )

    assert conflicts == (axis,)


@pytest.mark.parametrize(
    "field,value",
    [
        ("target_metrics", [{}]),
        ("requested_dimensions", [["channel"]]),
    ],
)
def test_route_typed_lists_reject_nested_values_as_llm_contract(field, value):
    from bi_agent.runtime import langgraph_workflow as workflow

    with pytest.raises(
        workflow.WorkflowFailure,
        match=f"analysis_route_contract_invalid:analysis_requirements:{field}",
    ) as exc:
        workflow._validate_route_analysis_requirements(
            {"analysis_requirements": {field: value}},
            _registry(),
        )

    assert exc.value.failure_type == "llm_contract"


@pytest.mark.parametrize(
    "invalid_requirements",
    [
        {
            "target_metrics": ["paid_amount"],
            "requested_dimensions": [{}],
        },
        {
            "target_metrics": ["paid_amount", "paid_amount"],
            "requested_dimensions": [],
        },
    ],
)
def test_route_typed_lists_fail_current_contract_validation(invalid_requirements):
    from bi_agent.runtime import langgraph_workflow as workflow

    with pytest.raises(
        workflow.WorkflowFailure,
        match="analysis_route_contract_invalid:analysis_requirements:",
    ) as exc:
        workflow._validate_route_analysis_requirements(
            {"analysis_requirements": invalid_requirements},
            _registry(),
        )

    assert exc.value.failure_type == "llm_contract"


def test_normalized_question_families_preserve_secondary_analysis_axis():
    from bi_agent.runtime import langgraph_workflow as workflow

    normalized = workflow._normalize_question_families(
        {
            "question_family": "data_quality_or_evidence_review",
            "primary_question_family": "data_quality_or_evidence_review",
            "question_families": ["data_quality_or_evidence_review"],
            "secondary_question_families": ["segment_or_factor_attribution"],
        }
    )
    assert normalized["question_families"] == [
        "data_quality_or_evidence_review",
        "segment_or_factor_attribution",
    ]
    obligation = _registry().question_family_obligation(
        normalized["question_families"][1]
    )
    capabilities = {
        *obligation.get("required_capabilities", ()),
        *obligation.get("independent_capabilities", ()),
        *(
            capability
            for rule in obligation.get("conditional_rules", ())
            for capability in rule.get("add", ())
        ),
    }
    assert "gameplay_activity_context" in capabilities


def test_queryless_signed_plans_are_terminal_without_query_execution():
    from bi_agent.runtime.analysis_contract_compiler import compile_analysis_contract
    from bi_agent.runtime.dataset_catalog import DatasetCatalog
    from tools.phase7.run_live_conversation_system_test import (
        _derive_plan_capability_outcomes,
    )

    outcome = compile_analysis_contract(
        run_id="run-queryless-terminal",
        proposal={"target_metrics": ["paid_amount"]},
        accepted_capabilities=("answer_verify", "evidence_reduce"),
        catalog=DatasetCatalog(()),
        registry=_registry(),
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        permission_scope="analyst",
    )
    authority = {
        "run_id": "run-queryless-terminal",
        "checkpoint_events": [
            {"node": "reduce_evidence", "status": "failed"},
            {"node": "reduce_evidence", "status": "completed"},
        ],
        "admin_audit": {
            "analysis_contract": outcome.analysis_contract.to_dict(),
            "compiler_runtime_plan": {
                "analysis_contract": outcome.analysis_contract.to_dict()
            },
            "verifier": {"status": "passed", "errors": []},
            "capability_execution_plans": [
                asdict(plan) for plan in outcome.capability_plans
            ],
        }
    }

    assert _derive_plan_capability_outcomes(authority, _registry()) == {
        "answer_verify": {"executed"},
        "evidence_reduce": {"executed"},
    }

    assert _derive_plan_capability_outcomes(
        {
            **authority,
            "checkpoint_events": [],
            "admin_audit": {
                **authority["admin_audit"],
                "verifier": {"status": "failed", "errors": ["blocked"]},
            },
        },
        _registry(),
    ) == {}


@pytest.mark.parametrize(
    "completion_authority",
    ["", "client_flag", "checkpoint_completed:", "checkpoint_completed:Bad-Node"],
)
def test_queryless_completion_authority_contract_rejects_invalid_grammar(
    completion_authority,
):
    from copy import deepcopy
    from bi_agent.runtime.contracts import load_contract

    payload = deepcopy(load_contract(CANONICAL_RUNTIME_BINDINGS_PATH))
    payload["capability_inputs"]["answer_verify"][
        "completion_authority"
    ] = completion_authority

    with pytest.raises(ValueError, match="runtime_capability_completion_authority"):
        RuntimeContractRegistry(payload)


def test_capability_authority_is_conservative_across_ambiguous_applicable_cells(
    tmp_path, monkeypatch
):
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    authority = _executed_market_authority(tmp_path, monkeypatch)
    admin_audit = authority["admin_audit"]
    admin_audit["capability_bindings"][-1]["status"] = "degraded"
    admin_audit["query_executions"][-1].update(
        completeness_status="partial", analysis_readiness="degraded"
    )
    review = review_case_obligations(
        {
            "status": "completed",
            "answer_package": {"summary": "authority-backed terminal answer"},
            "accepted_graph": [
                "data_quality_profile",
                "formula_decompose",
                "market_health_compare",
            ],
            "scenario": {
                "question_family": "revenue_health_review",
                "target_metrics": ["paid_amount"],
                "expected_capability_states": {
                    "market_health_compare": "executable"
                },
                "expected_dataset_states": {"market_dashboard": "executable"},
                "allowed_claim_ceiling": "directional",
                "terminal_boundary": "verified_answer",
            },
            "runtime_authority": authority,
        },
        _registry(),
        coverage_authority={
            "cells": {
                "market_health_compare:market_dashboard": {
                    "capability": "market_health_compare",
                    "datasets": ["market_dashboard"],
                    "question_families": ["revenue_health_review"],
                    "state": "executable",
                },
                "market_health_compare:market_dashboard_secondary": {
                    "capability": "market_health_compare",
                    "datasets": ["market_dashboard"],
                    "question_families": ["revenue_health_review"],
                    "state": "contract_partial",
                },
            }
        },
    )

    assert review["expected_capability_states"] == {
        "market_health_compare": "contract_partial"
    }
    assert review["ambiguous_authority_capabilities"] == ["market_health_compare"]
    assert review["capability_state_mismatches"] == [
        "market_health_compare:contract_partial->unobserved"
    ]


def test_capability_authority_does_not_bind_an_unrelated_dataset_role(
    tmp_path, monkeypatch
):
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    review = review_case_obligations(
        {
            "status": "completed",
            "answer_package": {"summary": "authority-backed terminal answer"},
            "accepted_graph": [
                "data_quality_profile",
                "formula_decompose",
                "market_health_compare",
            ],
            "scenario": {
                "question_family": "revenue_health_review",
                "target_metrics": ["paid_amount"],
                "expected_capability_states": {
                    "market_health_compare": "executable"
                },
                "expected_dataset_states": {"market_dashboard": "executable"},
                "allowed_claim_ceiling": "directional",
                "terminal_boundary": "verified_answer",
            },
            "runtime_authority": _executed_market_authority(tmp_path, monkeypatch),
        },
        _registry(),
        coverage_authority={
            "cells": {
                "market_health_compare:other_dataset": {
                    "capability": "market_health_compare",
                    "datasets": ["other_dataset"],
                    "question_families": ["revenue_health_review"],
                    "state": "executable",
                },
                "data_quality_profile:market_dashboard": {
                    "capability": "data_quality_profile",
                    "datasets": ["market_dashboard"],
                    "question_families": ["revenue_health_review"],
                    "state": "executable",
                },
            }
        },
    )

    assert review["unresolved_authority_capabilities"] == [
        "market_health_compare"
    ]
    assert review["hard_acceptance_passed"] is False
