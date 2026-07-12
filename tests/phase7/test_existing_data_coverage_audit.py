from dataclasses import replace
from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from bi_agent.runtime.analysis_contracts import AnalysisContract, ContractGap
from bi_agent.runtime.current_data_coverage import current_data_coverage_cases
from bi_agent.runtime.dataset_catalog import build_dataset_release_authority_record, dataset_snapshot_release_ref
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


def _analysis_contract_gap_authority(
    gaps: list[dict[str, object]], required_capabilities: list[str]
) -> dict[str, object]:
    typed_gaps = tuple(
        ContractGap(
            gap_type=str(gap["gap_type"]),
            gap_id=str(gap["gap_id"]),
            dataset_id=str(gap.get("dataset_id") or ""),
            affected_capabilities=tuple(required_capabilities),
            affected_claim_types=(),
            owner=str(gap["owner"]),
            repair_options=("repair_contract_boundary",),
            requires_clarification=False,
            diagnostic_context={},
        )
        for gap in gaps
    )
    return AnalysisContract(
        analysis_contract_id="analysis-contract:obligation-review-test",
        contract_version="1",
        question_families=(),
        target_metric_refs=(),
        claim_intents=(),
        scope={},
        business_timezone="Europe/London",
        as_of="2026-06-03T12:00:00+01:00",
        resolved_windows=(),
        metric_bindings=(),
        dimension_bindings=(),
        dataset_requirements=tuple(
            dict.fromkeys(
                str(gap.get("dataset_id") or "") for gap in gaps if gap.get("dataset_id")
            )
        ),
        capability_requirements=tuple(required_capabilities),
        permission_scope="analyst",
        contract_gaps=typed_gaps,
    ).to_dict()


def test_platform_suite_covers_public_families_current_roles_and_boundaries():
    from tools.phase7.run_live_conversation_system_test import load_cases

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    cases = load_cases("evals/phase7/existing_data_coverage_scenarios.yaml")
    scenarios = [turn for case in cases for turn in case["turns"]]

    assert {turn["scenario"]["question_family"] for turn in scenarios} == set(
        registry.question_family_ids
    )
    assert {
        "paid_order_success",
        "market_dashboard",
        "market_dashboard_channel",
        "gameplay",
        "gameplay_channel",
        "external_event",
    } <= {
        dataset
        for turn in scenarios
        for dataset in turn["scenario"]["expected_dataset_states"]
    }
    boundary_types = {
        turn["scenario"].get("terminal_boundary") for turn in scenarios
    }
    assert {"permission_blocked", "contract_allowed_partial"} <= boundary_types
    assert any(turn["scenario"].get("reuse") == "required" for turn in scenarios)
    assert any(
        turn["scenario"].get("clarification_resume") == "required"
        for turn in scenarios
    )
    assert not any(
        "final_answer_contains" in turn.get("expect", {})
        for turn in scenarios
    )


def test_suite_selector_keeps_fixed_eight_and_platform_tracks_distinct():
    from tools.phase7.run_live_conversation_system_test import load_suite_cases

    fixed = load_suite_cases("fixed-eight")
    platform = load_suite_cases("platform-current-data")

    assert [case["id"] for case in fixed] == [
        "paid_amount_revenue_diagnostics_8_question_set"
    ]
    assert len(fixed[0]["turns"]) == 8
    assert {case["group"] for case in platform} == {"platform_current_data"}


def test_platform_suite_applies_fixed_authority_clock_to_every_case():
    from tools.phase7.run_live_conversation_system_test import load_suite_cases

    contexts = [case.get("analysis_context") for case in load_suite_cases("platform-current-data")]
    assert contexts
    assert all(
        context == {
            "as_of": "2026-06-03T12:00:00+01:00",
            "target_date": "2026-06-02",
            "previous_day": "2026-06-01",
            "rolling_7_day_start": "2026-05-26",
            "rolling_7_day_end": "2026-06-01",
            "same_weekday_last_week": "2026-05-26",
            "pattern_history_start": "2026-01-01",
            "anomaly_history_start": "2026-05-03",
        }
        for context in contexts
    )


def test_all_suite_claim_ceilings_use_runtime_maximum_strength_taxonomy():
    from tools.phase7.run_live_conversation_system_test import load_suite_cases

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    for suite in ("fixed-eight", "platform-current-data"):
        for case in load_suite_cases(suite):
            for turn in case["turns"]:
                ceiling = str(
                    (turn.get("scenario") or {}).get("allowed_claim_ceiling") or ""
                )
                assert registry.maximum_claim_strength_rank(ceiling) >= 0


def test_obligation_review_resolves_contract_and_reports_typed_gaps():
    from tools.phase7.run_live_conversation_system_test import (
        review_case_obligations,
    )

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    turn = {
        "accepted_graph": [
            "data_quality_profile",
            "driver_decomposition",
            "compare_periods",
            "answer_verify",
            "metric_timeseries",
        ],
        "scenario": {
            "question_family": "paid_amount_change_explanation",
            "target_metrics": ["paid_amount"],
            "baselines": ["previous_day"],
            "required_capabilities": [],
            "expected_dataset_states": {
                "paid_order_success": "executable",
                "payment_attempt": "source_unbound",
            },
            "excluded_inputs": {
                "payment_attempt": "missing_contract",
            },
            "allowed_claim_ceiling": "directional",
            "terminal_boundary": "contract_allowed_partial",
        },
        "status": "completed",
        "runtime_authority": {
            "query_executions": [{"dataset_id": "paid_order_success", "execution_status": "succeeded", "completeness_status": "complete"}],
            "contract_gaps": [{"dataset_id": "payment_attempt", "gap_type": "missing_contract"}],
        },
    }

    review = review_case_obligations(turn, registry)

    assert review["missing_required_capabilities"] == []
    assert review["expected_typed_gaps"] == {
        "payment_attempt": "missing_contract"
    }
    assert review["missing_current_data_obligations"] == []
    assert "final_answer_contains" not in review


def test_obligation_review_fails_missing_current_data_obligation():
    from tools.phase7.run_live_conversation_system_test import (
        review_case_obligations,
    )

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    review = review_case_obligations(
        {
            "accepted_graph": ["data_quality_profile", "answer_verify"],
            "scenario": {
                "question_family": "data_quality_or_evidence_review",
                "target_metrics": ["paid_amount"],
                "expected_dataset_states": {"paid_order_success": "executable"},
                "allowed_claim_ceiling": "directional",
                "terminal_boundary": "verified_answer",
            },
            "status": "completed",
            "runtime_authority": {},
        },
        registry,
    )

    assert review["missing_current_data_obligations"] == [
        "paid_order_success:executable"
    ]
    assert review["hard_acceptance_passed"] is False


@pytest.mark.parametrize(
    ("boundary", "gaps", "claim_strength", "status", "passed"),
    [
        ("verified_answer", [], "observed", "completed", True),
        ("verified_answer", [], "strong", "completed", False),
        ("permission_blocked", [{"dataset_id": "market_dashboard_channel", "gap_type": "permission_blocked"}], "insufficient", "completed", True),
        ("permission_blocked", [], "insufficient", "completed", False),
        ("contract_allowed_partial", [{"dataset_id": "gameplay_channel", "gap_type": "contract_partial"}], "context_only", "completed", True),
        ("contract_allowed_partial", [], "context_only", "completed", False),
    ],
)
def test_obligation_review_enforces_claim_ceiling_and_terminal_boundary(
    boundary, gaps, claim_strength, status, passed
):
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    dataset = "market_dashboard_channel" if boundary == "permission_blocked" else "gameplay_channel"
    expected_state = "permission_blocked" if boundary == "permission_blocked" else "contract_partial"
    if boundary == "verified_answer":
        dataset, expected_state = "paid_order_success", "executable"
    required_capabilities = [
        "data_quality_profile",
        "answer_verify",
        "metric_coverage_profile",
    ]
    authority_gaps = [
        {
            **gap,
            "gap_id": (
                f"dataset:{gap['dataset_id']}:{gap['gap_type']}"
                + (":required_fields" if gap["gap_type"] == "contract_partial" else "")
            ),
            "affected_capabilities": required_capabilities,
            "owner": "contract_owner",
        }
        for gap in gaps
    ]
    turn = {
        "status": status,
        "accepted_graph": required_capabilities,
        "scenario": {
            "question_family": "data_quality_or_evidence_review",
            "target_metrics": ["paid_amount"],
            "allowed_claim_ceiling": "directional",
            "terminal_boundary": boundary,
            "expected_dataset_states": {dataset: expected_state},
            "excluded_inputs": ({dataset: gaps[0]["gap_type"]} if gaps else {}),
        },
        "runtime_authority": {
            "query_executions": ([{"dataset_id": dataset, "result_ref": "result:test", "execution_status": "succeeded", "completeness_status": "complete", "analysis_readiness": "ready"}] if boundary == "verified_answer" else []),
            "analysis_contract": _analysis_contract_gap_authority(
                authority_gaps, required_capabilities
            ),
            "capability_bindings": [
                {
                    "binding_manifest_ref": (
                        "binding:test"
                        if capability_id == "data_quality_profile"
                        else f"binding:{capability_id}"
                    ),
                    "capability_id": capability_id,
                    "maximum_claim_strength": "directional",
                    "result_refs": ["result:test"],
                    "status": "ready",
                }
                for capability_id in required_capabilities
            ],
            "evidence_manifests": [{"evidence_ref": "evidence:test", "binding_manifest_ref": "binding:test", "result_refs": ["result:test"]}],
            "verified_claims": [{"claim_ref": "claim:test", "claim_strength": claim_strength, "evidence_refs": ["evidence:test"], "result_refs": ["result:test"]}],
        },
    }
    review = review_case_obligations(turn, registry)
    assert review["claim_ceiling_passed"] is (claim_strength != "strong")
    assert review["terminal_boundary_passed"] is passed if claim_strength != "strong" else review["terminal_boundary_passed"]
    assert review["hard_acceptance_passed"] is passed


def test_claim_ceiling_uses_only_claim_producing_binding_provenance():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    def review(strength="observed", evidence_ref="evidence:analysis"):
        return review_case_obligations(
            {
                "status": "completed",
                "answer_package": {"final_answer": "有证据的结论"},
                "accepted_graph": ["data_quality_profile", "metric_coverage_profile", "answer_verify"],
                "scenario": {
                    "question_family": "data_quality_or_evidence_review",
                    "target_metrics": ["paid_amount"],
                    "expected_dataset_states": {"paid_order_success": "executable"},
                    "allowed_claim_ceiling": "directional",
                    "terminal_boundary": "verified_answer",
                },
                "runtime_authority": {
                    "query_executions": [{"dataset_id": "paid_order_success", "execution_status": "succeeded", "completeness_status": "complete"}],
                    "capability_bindings": [
                        {"binding_manifest_ref": "binding:analysis", "capability_id": "data_quality_profile", "maximum_claim_strength": "directional", "result_refs": ["result:analysis"], "status": "ready"},
                        {"binding_manifest_ref": "binding:verify", "capability_id": "answer_verify", "maximum_claim_strength": "verifier_only", "result_refs": [], "status": "ready"},
                        {"binding_manifest_ref": "binding:reduce", "capability_id": "evidence_reduce", "maximum_claim_strength": "reducer_only", "result_refs": [], "status": "ready"},
                    ],
                    "evidence_manifests": [{"evidence_ref": "evidence:analysis", "binding_manifest_ref": "binding:analysis", "result_refs": ["result:analysis"]}],
                    "verified_claims": [{"claim_ref": "claim:1", "claim_strength": strength, "evidence_refs": [evidence_ref], "result_refs": (["result:analysis"] if evidence_ref == "evidence:analysis" else ["result:missing"])}],
                },
            },
            registry,
        )

    legal = review()
    assert legal["claim_ceiling_passed"] is True
    assert legal["actual_authority_ceiling"] == "directional"
    assert legal["missing_claim_capability_provenance"] == []
    assert review("strong")["claim_ceiling_passed"] is False
    missing = review(evidence_ref="evidence:missing")
    assert missing["missing_claim_capability_provenance"] == ["claim:1"]
    assert missing["hard_acceptance_passed"] is False


def test_runtime_review_serializes_same_hard_acceptance_summary(tmp_path):
    from tools.phase7.run_live_conversation_system_test import _write_case_artifact

    output = {
        "coverage_summary": {
            "hard_acceptance": {"runtime_passed": True, "obligation_passed": False, "passed": False},
            "runtime_correctness": {"all_required_queries_complete": True},
        },
        "turns": [{"index": 1, "real_clickhouse_review": {}, "obligation_review": {}}],
    }
    _write_case_artifact(tmp_path, "serialized-hard", output)
    runtime = json.loads((tmp_path / "serialized-hard.runtime-review.json").read_text())
    coverage = json.loads((tmp_path / "serialized-hard.coverage-summary.json").read_text())
    assert runtime["hard_acceptance"] == coverage["hard_acceptance"]


def test_runtime_observation_does_not_copy_expected_and_requires_excluded_gap():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    base = {
        "status": "completed",
        "accepted_graph": ["data_quality_profile"],
        "scenario": {
            "question_family": "business_object_impact_review",
            "target_metrics": ["paid_amount"],
            "expected_dataset_states": {"gameplay_channel": "contract_partial"},
            "excluded_inputs": {"gameplay_channel": "contract_partial"},
            "allowed_claim_ceiling": "candidate_mechanism",
            "terminal_boundary": "contract_allowed_partial",
        },
        "runtime_authority": {"contract_gaps": []},
    }
    review = review_case_obligations(base, registry)
    assert review["observed_dataset_states"] == {}
    assert review["missing_current_data_obligations"] == ["gameplay_channel:contract_partial"]
    assert review["missing_expected_typed_gaps"] == ["gameplay_channel:contract_partial"]
    assert review["hard_acceptance_passed"] is False


def test_obligation_review_resolves_current_state_from_release_authority():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    turn = {
        "status": "completed",
        "accepted_graph": [
            "metric_coverage_profile",
            "data_quality_profile",
            "answer_verify",
        ],
        "scenario": {
            "question_family": "data_quality_or_evidence_review",
            "target_metrics": ["paid_amount"],
            "expected_dataset_states": {"paid_order_success": "executable"},
            "allowed_claim_ceiling": "trust_boundary",
            "terminal_boundary": "contract_allowed_partial",
        },
        "runtime_authority": {
            "analysis_contract": _analysis_contract_gap_authority([{
                "dataset_id": "paid_order_success",
                "gap_type": "dataset_snapshot_unavailable_as_of",
                "gap_id": "dataset:paid_order_success:dataset_snapshot_unavailable_as_of",
                "owner": "data_owner",
            }], [
                "metric_coverage_profile",
                "data_quality_profile",
                "answer_verify",
            ]),
        },
    }
    coverage_authority = {
        "as_of": "2026-06-03T12:00:00+01:00",
        "permission_scope": "analyst",
        "cells": {
            "data_quality_profile:paid_order_success": {
                "capability": "data_quality_profile",
                "datasets": ["paid_order_success"],
                "question_families": ["data_quality_or_evidence_review"],
                "state": "snapshot_unavailable_as_of",
            },
        },
    }

    review = review_case_obligations(
        turn,
        registry,
        coverage_authority=coverage_authority,
    )

    assert review["authored_expected_dataset_states"] == {
        "paid_order_success": "executable"
    }
    assert review["expected_dataset_states"] == {
        "paid_order_success": "snapshot_unavailable_as_of"
    }
    assert review["authored_authority_mismatches"] == [
        "paid_order_success:executable->snapshot_unavailable_as_of"
    ]
    assert review["expected_typed_gaps"] == {
        "paid_order_success": "snapshot_unavailable_as_of"
    }
    assert review["missing_current_data_obligations"] == []
    assert review["hard_acceptance_passed"] is True


def test_obligation_review_fails_closed_when_declared_role_has_no_authority_cell():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    review = review_case_obligations(
        {
            "status": "completed",
            "accepted_graph": ["data_quality_profile", "answer_verify"],
            "scenario": {
                "question_family": "data_quality_or_evidence_review",
                "target_metrics": ["paid_amount"],
                "expected_dataset_states": {"paid_order_success": "executable"},
                "allowed_claim_ceiling": "trust_boundary",
                "terminal_boundary": "verified_answer",
            },
            "runtime_authority": {},
        },
        registry,
        coverage_authority={"cells": {}},
    )

    assert review["unresolved_authority_dataset_roles"] == ["paid_order_success"]
    assert review["hard_acceptance_passed"] is False


def test_authority_resolution_uses_dataset_role_cell_when_route_is_independent():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    review = review_case_obligations(
        {
            "status": "completed",
            "accepted_graph": ["pattern_scan", "segment_contribution", "joint_attribution"],
            "scenario": {
                "question_family": "pattern_explanation",
                "target_metrics": ["paid_amount"],
                "requested_dimensions": ["channel"],
                "expected_dataset_states": {"market_dashboard_channel": "executable"},
                "allowed_claim_ceiling": "directional",
                "terminal_boundary": "contract_allowed_partial",
            },
            "runtime_authority": {
                "contract_gaps": [{
                    "dataset_id": "market_dashboard_channel",
                    "gap_type": "contract_partial",
                }]
            },
        },
        registry,
        coverage_authority={
            "cells": {
                "market_pattern_compare:market_dashboard_channel": {
                    "capability": "market_pattern_compare",
                    "datasets": ["market_dashboard_channel"],
                    "question_families": [],
                    "state": "contract_partial",
                }
            }
        },
    )

    assert review["unresolved_authority_dataset_roles"] == []
    assert review["ambiguous_authority_dataset_roles"] == [
        "market_dashboard_channel"
    ]
    assert review["expected_dataset_states"] == {
        "market_dashboard_channel": "contract_partial"
    }


def test_ambiguous_dataset_role_resolution_uses_conservative_authority_state():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    review = review_case_obligations(
        {
            "status": "completed",
            "accepted_graph": ["compare_periods", "driver_decomposition"],
            "scenario": {
                "question_family": "paid_amount_change_explanation",
                "target_metrics": ["paid_amount"],
                "expected_dataset_states": {"market_dashboard": "executable"},
                "allowed_claim_ceiling": "directional",
                "terminal_boundary": "contract_allowed_partial",
            },
            "runtime_authority": {
                "contract_gaps": [{
                    "dataset_id": "market_dashboard",
                    "gap_type": "contract_partial",
                }]
            },
        },
        registry,
        coverage_authority={
            "cells": {
                "market_health_compare:market_dashboard": {
                    "capability": "market_health_compare",
                    "datasets": ["market_dashboard"],
                    "question_families": [],
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

    assert review["expected_dataset_states"] == {
        "market_dashboard": "contract_partial"
    }
    assert review["ambiguous_authority_dataset_roles"] == ["market_dashboard"]
    assert review["missing_current_data_obligations"] == []


def test_coverage_summary_counts_declared_clarification_and_exact_reuse_only():
    from tools.phase7.run_live_conversation_system_test import _coverage_summary

    turns = [
        {
            "topic_id": "topic-1",
            "resumed_topic_id": "topic-1",
            "resumed_status": "completed",
            "scenario": {"clarification_resume": "required"},
            "obligation_review": {"hard_acceptance_passed": True, "clarification_resume_passed": True},
            "quality_review": {"display_status": "", "direct_answer": False},
            "real_clickhouse_review": {"runtime_correctness": {"all_required_queries_complete": True, "all_capabilities_bound": True, "all_claims_traceable": True}},
        },
        {
            "topic_id": "topic-1",
            "prior_topic_id": "topic-1",
            "scenario": {"reuse": "required"},
            "runtime_authority": {"reuse_decisions": [{"decision": "reuse"}]},
            "obligation_review": {"hard_acceptance_passed": True, "reuse_passed": True},
            "quality_review": {"display_status": "ready", "direct_answer": True},
            "real_clickhouse_review": {"runtime_correctness": {"all_required_queries_complete": True, "all_capabilities_bound": True, "all_claims_traceable": True}},
        },
        {"topic_id": "topic-2", "resumed_topic_id": "topic-2", "resumed_status": "completed", "scenario": {}},
    ]
    summary = _coverage_summary(turns)
    assert summary["final_answer_audit_coverage"] == {"reviewed": 0, "total": 3}
    assert summary["clarification_resume"] == {"required": 1, "passed": 1}
    assert summary["reuse_coverage"] == {"required": 1, "passed": 1}
    turns[0]["resumed_status"] = "failed"
    turns[0]["obligation_review"]["hard_acceptance_passed"] = False
    turns[1]["runtime_authority"]["reuse_decisions"][0]["decision"] = "rerun"
    turns[1]["obligation_review"]["hard_acceptance_passed"] = False
    failed = _coverage_summary(turns)
    assert failed["clarification_resume"] == {"required": 1, "passed": 0}
    assert failed["reuse_coverage"] == {"required": 1, "passed": 0}


def test_coverage_summary_reads_run_matched_internal_audit_for_zero_claim_terminal(
    tmp_path, monkeypatch
):
    from tools.phase7.run_live_conversation_system_test import _coverage_summary

    artifact_root = tmp_path / "artifacts"
    internal_path = artifact_root / "phase-7" / "run-boundary-only" / "answer_package.json"
    internal_path.parent.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    internal_path.write_text(
        json.dumps(
            {
                "run_id": "run-boundary-only",
                "final_answer": "当前数据边界不足，已给出负责方与下一步。",
                "quality_gate": {
                    "display_status": "ready",
                    "has_verified_claims": False,
                    "blocks_display": False,
                },
                "llm_calls": [
                    {
                        "task": "final_answer_audit",
                        "structured_output": {
                            "display_status": "ready",
                            "hard_blockers": [],
                            "repairable_warnings": [],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    turns = [
        {
            "resumed_status": "completed",
            "resumed_run_id": "run-boundary-only",
            "resumed_artifact_path": str(internal_path),
            "resumed_answer_package": {
                "run_id": "run-boundary-only",
                "final_answer": "当前数据边界不足，已给出负责方与下一步。",
                "quality_gate": {
                    "display_status": "",
                    "has_verified_claims": False,
                },
                "llm_calls": [],
            },
        }
    ]

    summary = _coverage_summary(turns)

    assert summary["final_answer_audit_coverage"] == {"reviewed": 1, "total": 1}

    turns[0]["resumed_quality_review"] = {"display_status": "ready"}
    turns[0]["resumed_run_id"] = "run-different"
    mismatched = _coverage_summary(turns)
    assert mismatched["final_answer_audit_coverage"] == {"reviewed": 0, "total": 1}


def test_coverage_summary_rejects_internal_audit_path_outside_artifact_root(
    tmp_path, monkeypatch
):
    from tools.phase7.run_live_conversation_system_test import _coverage_summary

    (tmp_path / "artifacts").mkdir()
    outside_path = tmp_path / "outside-answer-package.json"
    outside_path.write_text(
        json.dumps(
            {
                "run_id": "run-outside",
                "llm_calls": [
                    {
                        "task": "final_answer_audit",
                        "structured_output": {"display_status": "ready"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    turns = [
        {
            "status": "completed",
            "run_id": "run-outside",
            "artifact_path": "artifacts/../outside-answer-package.json",
            "quality_review": {"display_status": "ready"},
        }
    ]

    summary = _coverage_summary(turns)

    assert summary["final_answer_audit_coverage"] == {"reviewed": 0, "total": 1}


def test_coverage_summary_separates_expected_from_observed_dataset_states():
    from tools.phase7.run_live_conversation_system_test import _coverage_summary

    summary = _coverage_summary([
        {
            "obligation_review": {
                "required_capabilities": ["compare_periods"],
                "missing_required_capabilities": [],
                "expected_dataset_states": {
                    "paid_order_success": "executable",
                    "payment_attempt": "source_unbound",
                },
                "observed_dataset_states": {
                    "paid_order_success": "executable",
                },
                "missing_current_data_obligations": [
                    "payment_attempt:source_unbound"
                ],
                "hard_acceptance_passed": False,
            },
            "real_clickhouse_review": {"runtime_correctness": {}},
        }
    ])

    assert summary["expected_dataset_coverage"] == {
        "paid_order_success": {"executable": 1},
        "payment_attempt": {"source_unbound": 1},
    }
    assert summary["observed_dataset_coverage"] == {
        "paid_order_success": {"executable": 1},
        "payment_attempt": {"unobserved": 1},
    }
    assert summary["dataset_coverage_deprecated"] == {
        "meaning": "expected_dataset_coverage",
        "coverage": summary["expected_dataset_coverage"],
    }


def test_obligation_coverage_outcomes_are_mutually_exclusive_and_authoritative():
    from tools.phase7.run_live_conversation_system_test import _coverage_summary

    summary = _coverage_summary([
        {
            "obligation_review": {
                "required_capabilities": ["ready", "partial", "unseen", "unrouted"],
                "capability_outcomes": {
                    "ready": "executed",
                    "partial": "degraded",
                    "unseen": "unobserved",
                    "unrouted": "missing_route",
                },
                "hard_acceptance_passed": False,
            },
            "real_clickhouse_review": {"runtime_correctness": {}},
        }
    ])

    assert summary["obligation_coverage"] == {
        "required": 4,
        "routed": 3,
        "terminal": 2,
        "executed": 1,
        "degraded": 1,
        "blocked": 0,
        "unobserved": 1,
        "missing_route": 1,
    }


def test_obligation_review_requires_binding_result_and_completeness_chain():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    turn = {
        "status": "completed",
        "accepted_graph": [
            "metric_coverage_profile",
            "data_quality_profile",
            "answer_verify",
        ],
        "scenario": {
            "question_family": "data_quality_or_evidence_review",
            "target_metrics": ["paid_amount"],
            "expected_dataset_states": {},
            "allowed_claim_ceiling": "trust_boundary",
            "terminal_boundary": "verified_answer",
        },
        "runtime_authority": {
            "query_executions": [
                {
                    "result_ref": "result:complete",
                    "execution_status": "succeeded",
                    "completeness_status": "complete",
                    "analysis_readiness": "ready",
                },
                {
                    "result_ref": "result:partial",
                    "execution_status": "succeeded",
                    "completeness_status": "partial",
                    "analysis_readiness": "degraded",
                },
            ],
            "capability_bindings": [
                {
                    "capability_id": "metric_coverage_profile",
                    "status": "ready",
                    "result_refs": ["result:complete"],
                },
                {
                    "capability_id": "data_quality_profile",
                    "status": "degraded",
                    "result_refs": ["result:partial"],
                },
                {
                    "capability_id": "answer_verify",
                    "status": "ready",
                    "result_refs": ["result:missing"],
                },
            ],
        },
    }

    review = review_case_obligations(turn, registry)

    assert review["capability_outcomes"] == {
        "metric_coverage_profile": "executed",
        "data_quality_profile": "degraded",
        "answer_verify": "unobserved",
    }


@pytest.mark.parametrize(
    ("accepted_graph", "runtime_authority", "expected_outcome"),
    [
        (["answer_verify"], {}, "unobserved"),
        ([], {}, "missing_route"),
        (
            [],
            {
                "contract_gaps": [{
                    "gap_type": "contract_partial",
                    "gap_id": "dataset:paid_order_success:contract_partial",
                    "dataset_id": "paid_order_success",
                    "affected_capabilities": ["different_capability"],
                    "owner": "contract_owner",
                }]
            },
            "missing_route",
        ),
    ],
)
def test_obligation_review_rejects_every_nonterminal_required_capability_outcome(
    accepted_graph, runtime_authority, expected_outcome
):
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    review = review_case_obligations(
        {
            "status": "completed",
            "answer_package": {"summary": "terminal boundary response"},
            "accepted_graph": accepted_graph,
            "scenario": {
                "required_capabilities": ["answer_verify"],
                "expected_dataset_states": {},
                "allowed_claim_ceiling": "trust_boundary",
                "terminal_boundary": "verified_answer",
            },
            "runtime_authority": runtime_authority,
        },
        registry,
    )

    assert review["capability_outcomes"] == {"answer_verify": expected_outcome}
    assert review["nonterminal_required_capabilities"] == ["answer_verify"]
    assert review["hard_acceptance_passed"] is False


@pytest.mark.parametrize("terminal_outcome", ["executed", "degraded", "blocked"])
def test_obligation_review_accepts_only_authority_backed_terminal_capability_outcomes(
    terminal_outcome
):
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    accepted_graph = [] if terminal_outcome == "blocked" else ["answer_verify"]
    if terminal_outcome == "blocked":
        runtime_authority = {
            "analysis_contract": _analysis_contract_gap_authority([{
                "gap_type": "contract_partial",
                "gap_id": "capability:answer_verify:required_query:slot:unbound",
                "dataset_id": "paid_order_success",
                "owner": "contract_owner",
            }], ["answer_verify"])
        }
    else:
        partial = terminal_outcome == "degraded"
        runtime_authority = {
            "query_executions": [{
                "result_ref": "result:answer-verify",
                "execution_status": "succeeded",
                "completeness_status": "partial" if partial else "complete",
                "analysis_readiness": "degraded" if partial else "ready",
            }],
            "capability_bindings": [{
                "capability_id": "answer_verify",
                "status": "degraded" if partial else "ready",
                "result_refs": ["result:answer-verify"],
            }],
        }
    review = review_case_obligations(
        {
            "status": "completed",
            "answer_package": {"summary": "terminal boundary response"},
            "accepted_graph": accepted_graph,
            "scenario": {
                "required_capabilities": ["answer_verify"],
                "expected_dataset_states": {},
                "allowed_claim_ceiling": "trust_boundary",
                "terminal_boundary": "verified_answer",
            },
            "runtime_authority": runtime_authority,
        },
        registry,
    )

    assert review["capability_outcomes"] == {"answer_verify": terminal_outcome}
    assert review["nonterminal_required_capabilities"] == []
    assert review["hard_acceptance_passed"] is True


def test_cli_case_selection_rejects_conflicts_cross_suite_and_unknown():
    from tools.phase7.run_live_conversation_system_test import resolve_cli_cases

    with pytest.raises(ValueError, match="eval_cli_source_conflict"):
        resolve_cli_cases("fixed-eight", "custom.yaml", None)
    with pytest.raises(ValueError, match="eval_case_not_in_suite"):
        resolve_cli_cases("fixed-eight", None, "platform_paid_amount_change")
    with pytest.raises(ValueError, match="eval_case_unknown"):
        resolve_cli_cases(None, "evals/phase7/conversation_scenarios.yaml", "absent")


def test_cli_selection_error_is_typed_and_nonzero(capsys):
    from tools.phase7.run_live_conversation_system_test import main

    with pytest.raises(SystemExit) as exc:
        main(["--suite", "fixed-eight", "--case", "platform_paid_amount_change"])
    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload == {
        "ok": False,
        "error_code": "eval_case_not_in_suite",
        "owner": "eval_operator",
        "impact": "no evaluation cases were executed",
    }


class Releases:
    def __init__(self, records):
        self.records = {record.release_ref: record for record in records}

    def resolve_dataset_release(self, release_ref):
        return self.records[release_ref]


def authority_inputs(registry):
    snapshots = {}
    for case in current_data_coverage_cases(registry):
        for snapshot in (case.snapshots or {}).values():
            snapshots.setdefault(snapshot.dataset_id, snapshot)
    selected = []
    records = []
    for dataset_id in ("paid_order_success", "market_dashboard", "market_dashboard_channel", "gameplay", "gameplay_channel", "external_event"):
        snapshot = snapshots[dataset_id]
        membership = tuple(registry.dataset(dataset_id)["release_membership"]["dataset_ids"])
        members = [snapshots[item] for item in membership]
        record = build_dataset_release_authority_record(
            tuple({**item.to_dict(), "requires_release": True} for item in members)
        )
        records.append(record)
        selected.append(replace(snapshot, authority_record_ref=record.authority_record_ref))
    return tuple(selected), Releases(records)


def releases_for(registry, snapshots):
    by_dataset = {item.dataset_id: item for item in snapshots}
    records = []
    normalized = {}
    seen = set()
    for snapshot in snapshots:
        membership = tuple(registry.dataset(snapshot.dataset_id)["release_membership"]["dataset_ids"])
        key = tuple(membership)
        if key in seen:
            continue
        seen.add(key)
        members = [by_dataset[item] for item in membership]
        release_ref = dataset_snapshot_release_ref(
            members[0].logical_snapshot_id, members[0].load_revision,
            tuple(item.snapshot_ref for item in members),
        )
        members = [replace(item, release_ref=release_ref, authority_record_ref="") for item in members]
        record = build_dataset_release_authority_record(tuple(
            {**item.to_dict(), "requires_release": True} for item in members
        ))
        records.append(record)
        normalized.update({item.dataset_id: replace(item, authority_record_ref=record.authority_record_ref) for item in members})
    return tuple(normalized[item.dataset_id] for item in snapshots), Releases(records)


def test_coverage_audit_reports_current_and_excluded_cells():
    from bi_agent.runtime.coverage_audit import audit_existing_data_coverage

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    snapshots, releases = authority_inputs(registry)
    audit = audit_existing_data_coverage(
        registry,
        snapshot_records=snapshots,
        release_resolver=releases,
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        permission_scope="analyst",
    )
    assert audit["states"] == [
        "executable", "degraded", "source_unbound", "contract_partial",
        "permission_blocked", "snapshot_unavailable_as_of",
    ]
    assert audit["cells"]["market_health_compare:market_dashboard"]["state"] == "executable"
    assert audit["cells"]["source_reconciliation:market_dashboard"]["state"] == "contract_partial"
    assert audit["cells"]["event_evidence:external_event"]["state"] == "executable"
    assert audit["cells"]["driver_decomposition:payment_attempt"]["state"] == "source_unbound"
    excluded = audit["cells"]["event_evidence:internal_operation_event"]
    assert excluded["owner"] == "data_operations_owner"
    required = {"question_families", "capability", "datasets", "metrics", "dimensions", "windows", "evidence_types", "claim_ceiling", "current_release_refs", "state", "owner", "impact", "next_action"}
    assert required <= set(excluded)
    assert list(audit["cells"]) == sorted(audit["cells"])
    assert audit["cells"]["market_health_compare:market_dashboard"]["current_releases"][0]["load_revision"]


def test_coverage_audit_distinguishes_permission_future_and_partial_contract():
    from bi_agent.runtime.coverage_audit import audit_existing_data_coverage

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    snapshots, releases = authority_inputs(registry)
    changed = []
    for snapshot in snapshots:
        if snapshot.dataset_id == "market_dashboard":
            snapshot = replace(snapshot, permission_scopes=("admin",))
        elif snapshot.dataset_id == "external_event":
            snapshot = replace(snapshot, loaded_at="2026-06-04T00:00:00+00:00")
        changed.append(snapshot)
    changed, changed_releases = releases_for(registry, changed)
    audit = audit_existing_data_coverage(
        registry, changed, changed_releases,
        datetime.fromisoformat("2026-06-03T12:00:00+01:00"), "analyst",
    )
    assert audit["cells"]["market_health_compare:market_dashboard"]["state"] == "permission_blocked"
    assert audit["cells"]["event_evidence:external_event"]["state"] == "snapshot_unavailable_as_of"
    future = audit["cells"]["event_evidence:external_event"]
    assert "advance the audit as_of" in future["next_action"]
    assert "publish" not in future["next_action"]


def test_coverage_audit_fails_closed_on_release_integrity():
    from bi_agent.runtime.coverage_audit import audit_existing_data_coverage

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    snapshots, releases = authority_inputs(registry)
    releases.records[next(iter(releases.records))] = replace(
        next(iter(releases.records.values())), integrity_errors=("digest",)
    )
    with pytest.raises(ValueError, match="coverage_release_integrity"):
        audit_existing_data_coverage(registry, snapshots, releases, datetime.fromisoformat("2026-06-03T12:00:00+01:00"), "analyst")


class EmptyStore:
    def runtime_evidence_resolver(self):
        return object()

    def list_dataset_snapshots(self):
        return ()


def test_cli_writes_structurally_valid_source_unbound_artifact(tmp_path):
    from tools.phase7.audit_existing_data_coverage import run_audit

    output = tmp_path / "coverage.json"
    result = run_audit(SimpleNamespace(
        as_of="2026-06-03T12:00:00+01:00",
        permission_scope="analyst",
        out=str(output),
    ), store=EmptyStore())
    artifact = json.loads(output.read_text())
    assert result == {"ok": True, "artifact": str(output), "summary": artifact["summary"]}
    assert artifact["cells"]["driver_decomposition:payment_attempt"]["state"] == "source_unbound"


def test_cli_maps_credential_bearing_resolver_failure_without_disclosure(tmp_path, capsys):
    from tools.phase7.audit_existing_data_coverage import main

    secret = "postgresql://alice:password@secret-db.internal/waje"

    class BrokenStore:
        def runtime_evidence_resolver(self):
            return object()

        def list_dataset_snapshots(self):
            raise RuntimeError(f"connection failed {secret} SELECT * FROM private")

    code = main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(tmp_path / "coverage.json")],
        store_factory=lambda: BrokenStore(),
    )
    captured = capsys.readouterr()
    assert code == 1
    payload = json.loads(captured.err)
    assert payload == {
        "error_code": "coverage_database_unavailable",
        "impact": "current coverage authority could not be read",
        "ok": False,
        "owner": "runtime_operations_owner",
    }
    assert secret not in captured.err
    assert "secret-db" not in captured.err
    assert "SELECT" not in captured.err


def test_cli_maps_hard_release_integrity_failure_nonzero(tmp_path, capsys):
    from tools.phase7.audit_existing_data_coverage import main

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    snapshots, _ = authority_inputs(registry)

    class BrokenAuthorityStore(EmptyStore):
        def list_dataset_snapshots(self):
            return snapshots

        def resolve_dataset_release(self, release_ref):
            raise RuntimeError("postgresql://user:password@host/db")

    code = main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(tmp_path / "coverage.json")],
        store_factory=lambda: BrokenAuthorityStore(),
    )
    payload = json.loads(capsys.readouterr().err)
    assert code == 1
    assert payload["error_code"] == "coverage_release_authority_invalid"
    assert "password" not in json.dumps(payload)


def test_cli_maps_contract_integrity_failure_nonzero(tmp_path, capsys, monkeypatch):
    from tools.phase7 import audit_existing_data_coverage as cli

    def fail_contract(*args, **kwargs):
        raise ValueError("contract query SELECT secret_password")

    monkeypatch.setattr(cli.RuntimeContractRegistry, "from_path", fail_contract)
    code = cli.main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(tmp_path / "coverage.json")],
        store_factory=lambda: EmptyStore(),
    )
    payload = json.loads(capsys.readouterr().err)
    assert code == 1
    assert payload["error_code"] == "coverage_runtime_contract_invalid"
    assert "SELECT" not in json.dumps(payload)
    assert "password" not in json.dumps(payload)


def test_cli_maps_artifact_path_failure_without_echoing_path(tmp_path, capsys):
    from tools.phase7.audit_existing_data_coverage import main

    secret_path = tmp_path / "secret-password-output"
    secret_path.mkdir()
    code = main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(secret_path)],
        store_factory=lambda: EmptyStore(),
    )
    payload = json.loads(capsys.readouterr().err)
    assert code == 1
    assert payload["error_code"] == "coverage_artifact_write_failed"
    assert str(secret_path) not in json.dumps(payload)


def test_cli_maps_credential_bearing_close_failure_nonzero(tmp_path, capsys):
    from tools.phase7.audit_existing_data_coverage import main

    secret = "postgresql://closer:password@close-host.internal/waje"

    class Connection:
        def close(self):
            raise RuntimeError(f"close failed {secret} SELECT pg_terminate_backend")

    store = EmptyStore()
    store.connection = Connection()
    code = main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(tmp_path / "coverage.json")],
        store_factory=lambda: store,
    )
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.err)["error_code"] == "coverage_database_close_failed"
    assert captured.out == ""
    assert secret not in captured.err
    assert "close-host" not in captured.err
    assert "SELECT" not in captured.err


def test_cli_sanitizes_unknown_arguments_with_credentials(capsys):
    from tools.phase7.audit_existing_data_coverage import main

    secret = "postgresql://alice:password@host.internal/waje"
    code = main(["--bogus", secret], store_factory=lambda: EmptyStore())
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.err) == {
        "error_code": "coverage_cli_arguments_invalid",
        "impact": "the coverage audit command arguments are invalid",
        "ok": False,
        "owner": "audit_operator",
    }
    assert captured.out == ""
    assert secret not in captured.err
    assert "host.internal" not in captured.err
    assert "usage:" not in captured.err


def test_cli_preserves_primary_error_when_close_also_fails(tmp_path, capsys):
    from tools.phase7.audit_existing_data_coverage import main

    class Connection:
        def close(self):
            raise RuntimeError("postgresql://close:password@close-host/db")

    class BrokenStore(EmptyStore):
        connection = Connection()

        def list_dataset_snapshots(self):
            raise RuntimeError("postgresql://read:password@read-host/db SELECT secret")

    code = main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(tmp_path / "coverage.json")],
        store_factory=lambda: BrokenStore(),
    )
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.err)["error_code"] == "coverage_database_unavailable"
    assert "password" not in captured.err
    assert "host" not in captured.err
