import json
from dataclasses import asdict
from datetime import datetime

import pytest

from bi_agent.runtime.analysis_contracts import AnalysisContract
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


def _registry() -> RuntimeContractRegistry:
    return RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)


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
    return system_test._runtime_audit_package({
        "run_id": run_id,
        "artifact_path": str(path),
    })


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


def test_platform_matrix_exercises_executable_independent_result_authority():
    from tools.phase7.run_live_conversation_system_test import load_suite_cases
    from bi_agent.runtime.analysis_obligations import (
        ObligationRequest,
        resolve_analysis_obligations,
    )

    covered: set[tuple[str, str]] = set()
    for case in load_suite_cases("platform-current-data"):
        for turn in case["turns"]:
            scenario = turn["scenario"]
            resolution = resolve_analysis_obligations(
                ObligationRequest(
                    question_families=(scenario["question_family"],),
                    diagnostic_tags=tuple(scenario.get("diagnostic_tags") or ()),
                    target_metrics=tuple(scenario.get("target_metrics") or ()),
                    requested_dimensions=tuple(
                        scenario.get("requested_dimensions") or ()
                    ),
                    baselines=tuple(scenario.get("baselines") or ()),
                    context_sources=tuple(scenario.get("context_sources") or ()),
                    claim_intents=tuple(scenario.get("claim_intents") or ()),
                ),
                _registry(),
            )
            for capability in resolution.independent_capabilities:
                if scenario.get("expected_capability_states", {}).get(capability) == "executable":
                    covered.add((capability, scenario["question_family"]))

    assert ("market_health_compare", "revenue_health_review") in covered


def test_platform_obligations_compile_to_terminal_graph_closure():
    from bi_agent.runtime.analysis_obligations import (
        ObligationRequest,
        resolve_analysis_obligations,
    )
    from bi_agent.runtime.compiler import compile_graph
    from tools.phase7.run_live_conversation_system_test import load_suite_cases

    registry = _registry()
    for case in load_suite_cases("platform-current-data"):
        for turn in case["turns"]:
            scenario = turn["scenario"]
            family = scenario["question_family"]
            requirements = {
                key: list(scenario.get(key) or ())
                for key in (
                    "diagnostic_tags",
                    "target_metrics",
                    "requested_dimensions",
                    "baselines",
                    "context_sources",
                    "claim_intents",
                )
            }
            request = ObligationRequest(
                question_families=(family,),
                diagnostic_tags=tuple(requirements["diagnostic_tags"]),
                target_metrics=tuple(requirements["target_metrics"]),
                requested_dimensions=tuple(requirements["requested_dimensions"]),
                baselines=tuple(requirements["baselines"]),
                context_sources=tuple(requirements["context_sources"]),
                claim_intents=tuple(requirements["claim_intents"]),
            )
            resolution = resolve_analysis_obligations(request, registry)
            expected = set(
                (
                    *resolution.required_capabilities,
                    *resolution.conditional_capabilities,
                    *resolution.independent_capabilities,
                )
            )

            compiled = compile_graph(
                question_family=family,
                question_families=(family,),
                target_metric=requirements["target_metrics"][0],
                requested_nodes=("data_quality_profile",),
                bound_context={"analysis_requirements": requirements},
                runtime_registry=registry,
            )

            assert expected <= set(compiled.mutations.accepted_graph), (
                case["id"],
                expected - set(compiled.mutations.accepted_graph),
            )


def test_route_reconciliation_closes_all_obligations_idempotently():
    from bi_agent.runtime.langgraph_workflow import reconcile_analysis_route
    from tools.phase7.run_live_conversation_system_test import load_suite_cases

    registry = _registry()
    for case in load_suite_cases("platform-current-data"):
        for turn in case["turns"]:
            scenario = turn["scenario"]
            family = scenario["question_family"]
            requirements = {
                key: list(scenario.get(key) or ())
                for key in (
                    "diagnostic_tags",
                    "target_metrics",
                    "requested_dimensions",
                    "baselines",
                    "context_sources",
                    "claim_intents",
                )
            }
            intent = {
                "question_family": family,
                "question_families": [family],
                "target_metric": requirements["target_metrics"][0],
            }
            route = {"analysis_requirements": requirements}

            first, first_output = reconcile_analysis_route(
                ("data_quality_profile",), route, intent, registry
            )
            second, second_output = reconcile_analysis_route(
                first, first_output, intent, registry
            )

            assert second == first, case["id"]
            assert second_output["obligation_resolution"]["mutations"] == [], (
                case["id"],
                second_output["obligation_resolution"]["mutations"],
            )


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

    def invoke(state, node, payload):
        captured.update(payload)
        return {
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
        }

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


def test_business_intent_retries_missing_context_family_axis(monkeypatch):
    from bi_agent.runtime import langgraph_workflow as workflow

    outputs = [
        {
            "question_family": "data_quality_or_evidence_review",
            "question_families": ["data_quality_or_evidence_review"],
            "primary_question_family": "data_quality_or_evidence_review",
            "secondary_question_families": [],
        },
        {
            "question_family": "data_quality_or_evidence_review",
            "question_families": ["data_quality_or_evidence_review"],
            "primary_question_family": "data_quality_or_evidence_review",
            "secondary_question_families": [],
        },
        {
            "question_family": "business_object_impact_review",
            "question_families": [
                "business_object_impact_review",
                "data_quality_or_evidence_review",
            ],
            "primary_question_family": "business_object_impact_review",
            "secondary_question_families": ["data_quality_or_evidence_review"],
        },
    ]
    payloads = []

    def invoke(state, node, payload):
        payloads.append(payload)
        family = outputs.pop(0)
        return {
            **family,
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
        }

    monkeypatch.setattr(workflow, "_invoke_llm", invoke)
    state = {
        "run_id": "run-context-family-repair",
        "request": {"question": "arbitrary context question"},
        "checkpoint_events": [],
    }

    workflow._retrying_node(
        "understand_business_intent", workflow._understand_business_intent
    )(state)

    assert len(payloads) == 3
    for payload in payloads[1:]:
        feedback = payload["node_retry_feedback"]
        assert feedback["reason"] == "context_family_axis_missing:dimension:gameplay:gameplay"
        assert "business_object_impact_review" in feedback["correction"]
        assert "data_quality_or_evidence_review" in feedback["correction"]
    assert state["intent"]["question_family"] == "business_object_impact_review"
    assert state["intent"]["question_families"] == [
        "business_object_impact_review",
        "data_quality_or_evidence_review",
    ]
    assert state["intent"]["context_sources"] == []
    assert state["intent"]["requested_dimensions"] == ["gameplay"]


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


def test_business_intent_context_family_axis_fails_closed_after_retry(monkeypatch):
    from bi_agent.runtime import langgraph_workflow as workflow

    def invoke(state, node, payload):
        return {
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
        }

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

    assert [event["status"] for event in state["checkpoint_events"]] == [
        "retrying",
        "retrying",
        "failed",
    ]


def test_route_design_resolves_obligations_after_capability_family_inference(
    monkeypatch,
):
    from bi_agent.runtime import langgraph_workflow as workflow

    monkeypatch.setattr(
        workflow,
        "_invoke_llm",
        lambda state, node, payload: {
            "requested_nodes": ["segment_contribution"],
            "analysis_requirements": {
                "target_metrics": ["paid_amount"],
            },
        },
    )
    state = {
        "intent": {
            "question_family": "data_quality_or_evidence_review",
            "question_families": ["data_quality_or_evidence_review"],
            "primary_question_family": "data_quality_or_evidence_review",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
        },
        "confirmed_understanding": {},
        "request": {},
    }

    workflow._design_analysis_route(state)

    requested = set(state["analysis_route"]["requested_nodes"])
    assert {
        "gameplay_activity_context",
        "segment_breakdown",
        "segment_shift_compare",
    } <= requested
    assert "segment_or_factor_attribution" in state["intent"][
        "question_families"
    ]


def test_route_design_retries_metric_only_context_and_persists_repaired_context(
    monkeypatch,
):
    from bi_agent.runtime import langgraph_workflow as workflow

    outputs = ["market_dashboard", "gameplay"]
    payloads = []

    def invoke(state, node, payload):
        payloads.append(payload)
        return {
            "requested_nodes": ["gameplay_activity_context"],
            "analysis_requirements": {
                "target_metrics": ["paid_amount"],
                "context_sources": [outputs.pop(0)],
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
    workflow._retrying_node(
        "design_analysis_route", workflow._design_analysis_route
    )(state)

    assert payloads[0]["allowed_context_source_ids"]
    assert "node_retry_feedback" in payloads[1]
    assert state["analysis_route"]["analysis_requirements"]["context_sources"] == [
        "gameplay"
    ]
    assert [event["status"] for event in state["checkpoint_events"]] == [
        "retrying",
        "completed",
    ]

    with pytest.raises(
        workflow.WorkflowFailure,
        match="analysis_route_contract_invalid:analysis_requirements:baselines",
    ):
        workflow._validate_route_analysis_requirements(
            {"analysis_requirements": {"baselines": "previous_day"}},
            _registry(),
        )
    from bi_agent.runtime.analysis_contract_compiler import compile_analysis_contract
    from bi_agent.runtime.dataset_catalog import DatasetCatalog

    outcome = compile_analysis_contract(
        run_id="run-route-authority-closure",
        proposal=state["analysis_route"]["analysis_requirements"],
        accepted_capabilities=tuple(state["analysis_route"]["requested_nodes"]),
        catalog=DatasetCatalog(()),
        registry=_registry(),
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        permission_scope="analyst",
    )
    contract_capabilities = set(outcome.analysis_contract.capability_requirements)
    requested = set(state["analysis_route"]["requested_nodes"])
    planned = {plan.capability_id for plan in outcome.capability_plans}
    terminal = {
        capability
        for gap in outcome.analysis_contract.contract_gaps
        if gap.owner and gap.repair_options
        for capability in gap.affected_capabilities
    }
    assert requested <= contract_capabilities
    assert requested <= planned | terminal


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
