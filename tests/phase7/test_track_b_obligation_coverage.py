import json

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
