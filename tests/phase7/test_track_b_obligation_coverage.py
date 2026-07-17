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
