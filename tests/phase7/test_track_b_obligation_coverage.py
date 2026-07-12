from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


def _registry() -> RuntimeContractRegistry:
    return RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)


def _executed_market_authority() -> dict:
    capabilities = (
        "data_quality_profile",
        "formula_decompose",
        "market_health_compare",
    )
    return {
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


def test_obligation_review_unions_independently_executable_capabilities():
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
            "runtime_authority": _executed_market_authority(),
        },
        _registry(),
    )

    assert "market_health_compare" in review["independent_capabilities"]
    assert "market_health_compare" in review["required_capabilities"]
    assert review["capability_outcomes"]["market_health_compare"] == "executed"
    assert review["missing_required_capabilities"] == []


def test_independent_capability_selects_its_dataset_authority_cell_only():
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
            "runtime_authority": _executed_market_authority(),
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
            }
        },
    )

    assert review["expected_dataset_states"] == {"market_dashboard": "executable"}
    assert review["ambiguous_authority_dataset_roles"] == []
    assert review["hard_acceptance_passed"] is True


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


def test_capability_authority_is_conservative_across_ambiguous_applicable_cells():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    authority = _executed_market_authority()
    authority["capability_bindings"][-1]["status"] = "degraded"
    authority["query_executions"][-1].update(
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
    assert review["capability_state_mismatches"] == []


def test_capability_authority_does_not_bind_an_unrelated_dataset_role():
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
            "runtime_authority": _executed_market_authority(),
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
