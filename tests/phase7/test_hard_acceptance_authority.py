from __future__ import annotations

from datetime import datetime
import json

import pytest

from bi_agent.runtime.analysis_contracts import (
    AnalysisContract,
    ContractGap,
    analysis_contract_signature,
    analysis_contract_from_dict,
    query_contract_signature,
)
from bi_agent.runtime.dataset_catalog import DatasetCatalog
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


def _analysis_contract(*gaps: ContractGap) -> dict[str, object]:
    return AnalysisContract(
        analysis_contract_id="analysis-contract:test",
        contract_version="1",
        question_families=("data_quality_or_evidence_review",),
        target_metric_refs=(),
        claim_intents=(),
        scope={},
        business_timezone="Europe/London",
        as_of="2026-06-03T12:00:00+01:00",
        resolved_windows=(),
        metric_bindings=(),
        dimension_bindings=(),
        dataset_requirements=("paid_order_success",),
        capability_requirements=("answer_verify",),
        permission_scope="analyst",
        contract_gaps=tuple(gaps),
    ).to_dict()


def _canonical_gap() -> ContractGap:
    return ContractGap(
        gap_type="contract_partial",
        gap_id="capability:answer_verify:required_query:answer:unbound",
        dataset_id="paid_order_success",
        affected_capabilities=("answer_verify",),
        affected_claim_types=(),
        owner="contract_owner",
        repair_options=("bind_required_query_contract",),
        requires_clarification=False,
        diagnostic_context={},
    )


def _persisted_plan_authority() -> tuple[dict[str, object], RuntimeContractRegistry]:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    capability_id = "market_health_compare"
    policy = registry.capability_inputs(capability_id)
    query_ref = "query:run-plan-authority:1"
    accepted = tuple(policy["minimum_readiness"]["accepted_completeness"])
    query_family = str(policy["query_families"][0])
    required_windows = ("target_day",)
    plan = {
        "capability_id": capability_id,
        "capability_contract_ref": registry.capability_contract_ref(capability_id),
        "required_input_slots": [{
            "slot_id": query_family,
            "required": True,
            "query_contract_refs": [query_ref],
            "validation_query_contract_refs": [],
            "accepted_completeness": list(accepted),
            "required_fields": ["window_id", "paid_amount"],
            "required_window_ids": list(required_windows),
        }],
        "optional_input_slots": [],
        "merge_strategy": policy.get("merge_strategy") or "by_query_family",
        "minimum_readiness": dict(policy["minimum_readiness"]),
        "degradation_policy": dict(policy["degradation_policy"]),
        "supported_evidence_types": list(policy["supported_evidence_types"]),
        "maximum_claim_strength": policy["maximum_claim_strength"],
        "analysis_contract_ref": "analysis:run-plan-authority:1",
        "supported_claim_types": list(policy["supported_claim_types"]),
        "capability_contract_version": registry.contract_version,
        "capability_contract_signature": registry.capability_contract_signature(
            capability_id
        ),
        "claim_strength_taxonomy_version": registry.claim_strength_taxonomy_version,
        "maximum_claim_strength_rank": registry.maximum_claim_strength_rank(
            str(policy["maximum_claim_strength"])
        ),
    }
    query = {
        "query_contract_id": query_ref,
        "analysis_contract_ref": plan["analysis_contract_ref"],
        "query_intent": query_family,
        "dataset_snapshot_refs": ["snapshot:market"],
        "metric_bindings": [{
            "metric_id": "paid_amount",
            "contract_ref": "contract:paid_amount",
            "dataset_id": "market_dashboard",
            "expression": "sum(paid_amount)",
            "aggregation": "sum",
            "required_fields": ["paid_amount"],
            "grain": ["window_id"],
            "value_semantics": "raw_scalar",
            "display_format": "number",
            "claim_types": ["comparative_change"],
            "numerator_metric": "",
            "denominator_metric": "",
            "zero_denominator_policy": "null",
            "reconciliation_strategy": "additive_sum",
            "reconciliation_tolerance": 0.01,
        }],
        "dimension_bindings": [],
        "window_refs": list(required_windows),
        "resolved_windows": [],
        "filters": [],
        "result_shape": {
            "required_fields": ["window_id", "paid_amount"],
            "unique_key": ["window_id"],
            "grain": ["window_id"],
            "required_window_ids": list(required_windows),
            "result_semantics": "complete_aggregate",
            "dimension_presence_policy": "paired_required",
        },
        "completeness_assertions": ["execution_succeeded"],
        "permission_scope": "analyst",
        "workload_class": "interactive_aggregate",
        "query_parameters": {},
        "query_role_ref": "query-role:test",
        "reconciliation_binding": None,
        "join_expectation": None,
    }
    query["contract_signature"] = query_contract_signature(query)
    result_ref = "result:plan-authority"
    report_ref = "completeness:plan-authority"
    accepted_contract = _analysis_contract()
    accepted_contract.update({
        "analysis_contract_id": plan["analysis_contract_ref"],
        "capability_requirements": [capability_id],
        "contract_gaps": [],
    })
    return {
        "analysis_contract": accepted_contract,
        "capability_execution_plans": [plan],
        "query_contracts": [query],
        "query_results": [{
            "query_contract_ref": query_ref,
            "query_id": "clickhouse:plan-authority",
            "query_hash": "query-hash",
            "result_ref": result_ref,
            "rows_ref": "rows:plan-authority",
            "row_count": 1,
            "completeness_report_ref": report_ref,
            "execution_status": "succeeded",
            "observed_schema": {"window_id": "string", "paid_amount": "Decimal"},
            "observed_windows": ["target_day"],
            "observed_grain": ["window_id"],
            "source_snapshot_refs": ["snapshot:market"],
            "provider_stats": {},
            "failure_reason": "",
            "execution_attempt_ref": "attempt:plan-authority",
        }],
        "completeness_reports": [{
            "report_ref": report_ref,
            "query_contract_ref": query_ref,
            "result_ref": result_ref,
            "completeness_status": "complete",
            "analysis_readiness": "ready",
            "assertion_results": [{
                "assertion": "execution_succeeded",
                "passed": True,
                "failure_reasons": [],
                "details": {},
            }],
            "failure_reasons": [],
            "coverage_summary": {
                "row_count": 1,
                "required_windows": ["target_day"],
                "observed_windows": ["target_day"],
                "expected_grain": ["window_id"],
                "observed_grain": ["window_id"],
                "snapshot_ref": "snapshot:market",
                "snapshot_refs": ["snapshot:market"],
                "rows_ref": "rows:plan-authority",
            },
        }],
    }, registry


def _bind_run_matched_accepted_contract(
    authority: dict[str, object], contract: AnalysisContract
) -> None:
    run_id = "run-plan-authority"
    persisted = contract.to_dict()
    persisted["analysis_contract_id"] = f"analysis:{run_id}:1"
    authority["analysis_contract"] = persisted
    authority["run_id"] = run_id
    authority["admin_audit"] = {
        **authority,
        "analysis_contract": persisted,
        "compiler_runtime_plan": {
            "analysis_contract": json.loads(json.dumps(persisted)),
        },
    }


def test_obligation_review_uses_persisted_family_and_reports_authored_mismatch():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    authority, registry = _persisted_plan_authority()
    contract = dict(authority["analysis_contract"])
    contract["question_families"] = ["segment_or_factor_attribution"]
    authority["analysis_contract"] = contract
    turn = {
        "status": "completed",
        "accepted_graph": [
            "data_quality_profile",
            "answer_verify",
            "gameplay_activity_context",
            "segment_breakdown",
            "segment_shift_compare",
        ],
        "scenario": {
            "question_family": "business_object_impact_review",
            "required_capabilities": ["event_window_compare"],
            "allowed_claim_ceiling": "trust_boundary",
            "terminal_boundary": "verified_answer",
        },
        "runtime_authority": authority,
    }

    review = review_case_obligations(turn, registry)

    assert review["authored_question_family"] == "business_object_impact_review"
    assert review["question_family"] == "segment_or_factor_attribution"
    assert review["question_family_authority_status"] == "mismatch"
    assert review["question_family_mismatch"] is True
    assert review["required_capabilities"] == [
        "data_quality_profile",
        "answer_verify",
        "gameplay_activity_context",
        "segment_breakdown",
        "segment_shift_compare",
    ]
    assert review["authored_required_capabilities"] == ["event_window_compare"]
    assert review["authored_required_capability_mismatches"] == [
        "event_window_compare"
    ]
    assert review["required_capability_authority_diff"] == {
        "authored_only": ["event_window_compare"],
        "derived_only": [
            "data_quality_profile",
            "answer_verify",
            "gameplay_activity_context",
            "segment_breakdown",
            "segment_shift_compare",
        ],
    }


@pytest.mark.parametrize(
    ("families", "expected_status"),
    [
        ([], "missing"),
        (["unknown_family"], "invalid"),
        (
            ["business_object_impact_review", "business_object_impact_review"],
            "invalid_contract",
        ),
    ],
)
def test_obligation_review_fails_closed_without_one_valid_persisted_family(
    families, expected_status
):
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    authority, registry = _persisted_plan_authority()
    contract = dict(authority["analysis_contract"])
    contract["question_families"] = families
    authority["analysis_contract"] = contract

    review = review_case_obligations(
        {
            "status": "completed",
            "accepted_graph": ["data_quality_profile"],
            "scenario": {
                "question_family": "business_object_impact_review",
                "allowed_claim_ceiling": "trust_boundary",
                "terminal_boundary": "verified_answer",
            },
            "runtime_authority": authority,
        },
        registry,
    )

    assert review["question_family"] == ""
    assert review["question_family_authority_status"] == expected_status
    assert review["hard_acceptance_passed"] is False


@pytest.mark.parametrize(
    ("families", "expected_required", "shared_capability"),
    [
        (
            ["custom_baseline_comparison", "data_quality_or_evidence_review"],
            [
                "metric_coverage_profile",
                "data_quality_profile",
                "compare_periods",
                "answer_verify",
                "market_health_compare",
                "user_mix_contribution",
            ],
            "answer_verify",
        ),
        (
            [
                "paid_amount_change_explanation",
                "data_quality_or_evidence_review",
            ],
            [
                "metric_coverage_profile",
                "data_quality_profile",
                "driver_decomposition",
                "answer_verify",
                "metric_timeseries",
            ],
            "data_quality_profile",
        ),
    ],
)
def test_obligation_review_unions_ordered_persisted_family_set_with_provenance(
    families, expected_required, shared_capability
):
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    authority, registry = _persisted_plan_authority()
    contract = dict(authority["analysis_contract"])
    contract["question_families"] = families
    authority["analysis_contract"] = contract

    review = review_case_obligations(
        {
            "status": "completed",
            "accepted_graph": expected_required,
            "scenario": {
                "question_family": families[0],
                "target_metrics": ["paid_amount"],
                "allowed_claim_ceiling": "trust_boundary",
                "terminal_boundary": "verified_answer",
            },
            "runtime_authority": authority,
        },
        registry,
    )

    assert review["question_family"] == families[0]
    assert review["question_families"] == families
    assert review["question_family_authority_status"] == "matched"
    assert review["required_capabilities"] == expected_required
    assert review["capability_family_provenance"][shared_capability] == families


@pytest.mark.parametrize(
    ("artifact_state", "expected_error"),
    [
        ("missing", "artifact_missing"),
        ("corrupt", "artifact_invalid"),
        ("run_mismatch", "run_id_mismatch"),
        ("missing_expected_run_id", "run_id_missing"),
        ("missing_payload_run_id", "persisted_run_id_missing"),
        ("contract_id_mismatch", "persisted_analysis_contract_mismatch"),
        ("effective_contract_mismatch", "effective_analysis_contract_id_mismatch"),
        ("expected_run_id_nonstring", "run_id_invalid"),
        ("persisted_run_id_nonstring", "persisted_run_id_invalid"),
        ("effective_contract_nonmapping", "effective_analysis_contract_invalid"),
        ("effective_contract_missing_id", "effective_analysis_contract_invalid"),
    ],
)
def test_runtime_audit_package_never_falls_back_to_client_gap_authority(
    tmp_path, monkeypatch, artifact_state, expected_error
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    monkeypatch.setattr(system_test, "ROOT", tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    path = artifact_root / "answer_package.json"
    run_id = "run-expected"
    contract = {
        **_analysis_contract(_canonical_gap()),
        "analysis_contract_id": f"analysis:{run_id}:1",
    }
    payload = {
        "run_id": run_id,
        "admin_audit": {"analysis_contract": contract},
    }
    if artifact_state == "missing":
        pass
    elif artifact_state == "corrupt":
        path.write_text("{not-json", encoding="utf-8")
    elif artifact_state == "run_mismatch":
        path.write_text(json.dumps({**payload, "run_id": "run-other"}), encoding="utf-8")
    elif artifact_state == "missing_expected_run_id":
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif artifact_state == "missing_payload_run_id":
        path.write_text(json.dumps({**payload, "run_id": ""}), encoding="utf-8")
    elif artifact_state == "persisted_run_id_nonstring":
        path.write_text(json.dumps({**payload, "run_id": 123}), encoding="utf-8")
    elif artifact_state == "contract_id_mismatch":
        stale = dict(contract)
        stale["analysis_contract_id"] = "analysis:run-stale:1"
        path.write_text(
            json.dumps({**payload, "admin_audit": {"analysis_contract": stale}}),
            encoding="utf-8",
        )
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    effective_contract = (
        {**contract, "analysis_contract_id": "analysis:run-stale:1"}
        if artifact_state == "effective_contract_mismatch"
        else []
        if artifact_state == "effective_contract_nonmapping"
        else {}
        if artifact_state == "effective_contract_missing_id"
        else contract
    )
    result = {
        "artifact_path": str(path),
        "answer_package": {
            "artifact_path": str(path),
            "analysis_contract": effective_contract,
            "admin_audit": {
                "analysis_contract": _analysis_contract(_canonical_gap())
            },
        },
    }
    if artifact_state != "missing_expected_run_id":
        result["run_id"] = (
            123 if artifact_state == "expected_run_id_nonstring" else run_id
        )
    signature = analysis_contract_signature(
        analysis_contract_from_dict(contract)
    )

    def resolve_authority(_resolved_run_id):
        return {
            "run_id": run_id,
            "analysis_contract": {
                **contract,
                "contract_signature": signature,
            },
            "stored_contract_signature": signature,
        }

    assert system_test._runtime_audit_package(
        result,
        authority_resolver=resolve_authority,
    ) == {
        "_authority_error": expected_error
    }


def test_runtime_audit_package_rejects_client_path_fallback(tmp_path, monkeypatch):
    from tools.phase7 import run_live_conversation_system_test as system_test

    monkeypatch.setattr(system_test, "ROOT", tmp_path)
    (tmp_path / "artifacts").mkdir()

    assert system_test._runtime_audit_package({
        "run_id": "run-expected",
        "answer_package": {"artifact_path": "artifacts/client.json"},
    }) == {"_authority_error": "artifact_path_missing"}


def test_runtime_audit_package_resolves_completed_contract_by_run_when_artifact_only_has_ref(
    tmp_path, monkeypatch
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    monkeypatch.setattr(system_test, "ROOT", tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    run_id = "run-completed-resume"
    contract = {
        **_analysis_contract(_canonical_gap()),
        "analysis_contract_id": "analysis-contract:completed-resume:v2",
    }
    signature = analysis_contract_signature(
        analysis_contract_from_dict(contract)
    )
    package = {
        "run_id": run_id,
        "status": "completed",
        "sections": [{"id": "summary", "payload": {"claims": []}}],
        "admin_audit": {
            "analysis_runtime_persistence": {
                "status": "persisted",
                "analysis_contract_ref": contract["analysis_contract_id"],
                "verified_claim_refs": [],
            }
        },
    }
    path = artifact_root / "answer_package.json"
    path.write_text(json.dumps(package), encoding="utf-8")
    resolved_run_ids = []

    def resolve_authority(resolved_run_id):
        resolved_run_ids.append(resolved_run_id)
        return {
            "run_id": run_id,
            "analysis_contract": {
                **contract,
                "contract_signature": signature,
            },
            "stored_contract_signature": signature,
        }

    audited = system_test._runtime_audit_package(
        {
            "run_id": run_id,
            "status": "completed",
            "artifact_path": str(path),
            "answer_package": package,
        },
        authority_resolver=resolve_authority,
    )

    assert resolved_run_ids == [run_id]
    assert audited["run_id"] == run_id
    assert audited["sections"] == package["sections"]
    assert audited["admin_audit"]["analysis_contract"] == contract


@pytest.mark.parametrize(
    ("resolver_state", "expected_error"),
    [
        ("missing", "missing_runtime_authority_resolver"),
        ("record_missing", "persisted_analysis_contract_missing"),
        ("run_mismatch", "runtime_authority_run_id_mismatch"),
        ("signature_drift", "persisted_analysis_contract_signature_mismatch"),
    ],
)
def test_runtime_audit_package_fails_closed_on_invalid_run_authority(
    tmp_path, monkeypatch, resolver_state, expected_error
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    monkeypatch.setattr(system_test, "ROOT", tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    run_id = "run-authority-boundary"
    contract = {
        **_analysis_contract(_canonical_gap()),
        "analysis_contract_id": f"analysis:{run_id}:1",
    }
    signature = analysis_contract_signature(
        analysis_contract_from_dict(contract)
    )
    package = {
        "run_id": run_id,
        "admin_audit": {
            "analysis_runtime_persistence": {
                "status": "persisted",
                "analysis_contract_ref": contract["analysis_contract_id"],
            }
        },
    }
    path = artifact_root / "answer_package.json"
    path.write_text(json.dumps(package), encoding="utf-8")

    def resolve_authority(_resolved_run_id):
        if resolver_state == "record_missing":
            return None
        return {
            "run_id": "run-other" if resolver_state == "run_mismatch" else run_id,
            "analysis_contract": {
                **contract,
                "contract_signature": (
                    "0" * 64 if resolver_state == "signature_drift" else signature
                ),
            },
            "stored_contract_signature": signature,
        }

    audited = system_test._runtime_audit_package(
        {
            "run_id": run_id,
            "artifact_path": str(path),
            "answer_package": package,
        },
        authority_resolver=(
            None if resolver_state == "missing" else resolve_authority
        ),
    )

    assert audited == {"_authority_error": expected_error}


def test_runtime_audit_package_fails_closed_on_artifact_contract_drift(
    tmp_path, monkeypatch
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    monkeypatch.setattr(system_test, "ROOT", tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    run_id = "run-artifact-contract-drift"
    contract = {
        **_analysis_contract(_canonical_gap()),
        "analysis_contract_id": f"analysis:{run_id}:1",
    }
    drifted = {**contract, "question_families": ["revenue_health_review"]}
    signature = analysis_contract_signature(
        analysis_contract_from_dict(contract)
    )
    path = artifact_root / "answer_package.json"
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "admin_audit": {"analysis_contract": drifted},
            }
        ),
        encoding="utf-8",
    )

    assert system_test._runtime_audit_package(
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
    ) == {"_authority_error": "persisted_analysis_contract_mismatch"}


def test_runtime_audit_package_fails_closed_on_artifact_contract_ref_drift(
    tmp_path, monkeypatch
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    monkeypatch.setattr(system_test, "ROOT", tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    run_id = "run-artifact-contract-ref-drift"
    contract = {
        **_analysis_contract(_canonical_gap()),
        "analysis_contract_id": "analysis-contract:authority:9",
    }
    signature = analysis_contract_signature(
        analysis_contract_from_dict(contract)
    )
    path = artifact_root / "answer_package.json"
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "admin_audit": {
                    "analysis_runtime_persistence": {
                        "status": "persisted",
                        "analysis_contract_ref": "analysis-contract:stale:8",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert system_test._runtime_audit_package(
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
    ) == {
        "_authority_error": "persisted_analysis_contract_ref_mismatch"
    }


@pytest.mark.parametrize(
    "drift_location",
    ["result_root", "answer_root", "answer_admin"],
)
def test_runtime_audit_package_validates_every_client_contract_copy(
    tmp_path, monkeypatch, drift_location
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    monkeypatch.setattr(system_test, "ROOT", tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    run_id = "run-client-contract-copies"
    contract = {
        **_analysis_contract(_canonical_gap()),
        "analysis_contract_id": "analysis-contract:client-copies:1",
    }
    drifted = {**contract, "question_families": ["revenue_health_review"]}
    signature = analysis_contract_signature(
        analysis_contract_from_dict(contract)
    )
    artifact = {
        "run_id": run_id,
        "admin_audit": {
            "analysis_runtime_persistence": {
                "status": "persisted",
                "analysis_contract_ref": contract["analysis_contract_id"],
            }
        },
    }
    path = artifact_root / "answer_package.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    result = {
        "run_id": run_id,
        "artifact_path": str(path),
        "analysis_contract": contract,
        "answer_package": {
            "analysis_contract": contract,
            "admin_audit": {"analysis_contract": contract},
        },
    }
    if drift_location == "result_root":
        result["analysis_contract"] = drifted
    elif drift_location == "answer_root":
        result["answer_package"]["analysis_contract"] = drifted
    else:
        result["answer_package"]["admin_audit"]["analysis_contract"] = drifted

    assert system_test._runtime_audit_package(
        result,
        authority_resolver=lambda _run_id: {
            "run_id": run_id,
            "analysis_contract": {
                **contract,
                "contract_signature": signature,
            },
            "stored_contract_signature": signature,
        },
    ) == {"_authority_error": "effective_analysis_contract_mismatch"}


def test_runtime_audit_package_accepts_artifact_root_contract_as_only_copy(
    tmp_path, monkeypatch
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    monkeypatch.setattr(system_test, "ROOT", tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    run_id = "run-artifact-root-contract"
    contract = {
        **_analysis_contract(_canonical_gap()),
        "analysis_contract_id": "analysis-contract:artifact-root:1",
    }
    signature = analysis_contract_signature(
        analysis_contract_from_dict(contract)
    )
    path = artifact_root / "answer_package.json"
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "analysis_contract": contract,
                "admin_audit": {},
            }
        ),
        encoding="utf-8",
    )

    audited = system_test._runtime_audit_package(
        {"run_id": run_id, "artifact_path": str(path)},
        authority_resolver=lambda _run_id: {
            "run_id": run_id,
            "analysis_contract": {
                **contract,
                "contract_signature": signature,
            },
            "stored_contract_signature": signature,
        },
    )

    assert audited["admin_audit"]["analysis_contract"] == contract


def test_runtime_audit_package_rejects_drifted_artifact_root_contract_when_admin_copy_matches(
    tmp_path, monkeypatch
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    monkeypatch.setattr(system_test, "ROOT", tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    run_id = "run-artifact-root-drift"
    contract = {
        **_analysis_contract(_canonical_gap()),
        "analysis_contract_id": "analysis-contract:artifact-root:2",
    }
    drifted = {**contract, "question_families": ["revenue_health_review"]}
    signature = analysis_contract_signature(
        analysis_contract_from_dict(contract)
    )
    path = artifact_root / "answer_package.json"
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "analysis_contract": drifted,
                "admin_audit": {"analysis_contract": contract},
            }
        ),
        encoding="utf-8",
    )

    assert system_test._runtime_audit_package(
        {"run_id": run_id, "artifact_path": str(path)},
        authority_resolver=lambda _run_id: {
            "run_id": run_id,
            "analysis_contract": {
                **contract,
                "contract_signature": signature,
            },
            "stored_contract_signature": signature,
        },
    ) == {"_authority_error": "persisted_analysis_contract_mismatch"}


def test_postgres_runtime_authority_resolver_queries_unique_contract_by_run_only():
    from tools.phase7 import run_live_conversation_system_test as system_test

    run_id = "run-noncanonical-contract-ref"
    contract = {
        **_analysis_contract(_canonical_gap()),
        "analysis_contract_id": "analysis-contract:custom-version:7",
    }
    signature = analysis_contract_signature(
        analysis_contract_from_dict(contract)
    )

    class Store:
        def _fetchall(self, statement, params):
            assert "analysis_contract_id =" not in statement
            assert params == {"run_id": run_id}
            return [
                (
                    run_id,
                    signature,
                    {**contract, "contract_signature": signature},
                )
            ]

    resolver = system_test._runtime_authority_resolver_for_store(Store())

    assert resolver(run_id) == {
        "run_id": run_id,
        "analysis_contract": {
            **contract,
            "contract_signature": signature,
        },
        "stored_contract_signature": signature,
    }


def test_postgres_runtime_authority_resolver_rejects_ambiguous_contracts_for_run():
    from tools.phase7 import run_live_conversation_system_test as system_test

    run_id = "run-ambiguous-contract-authority"

    class Store:
        def _fetchall(self, statement, params):
            assert params == {"run_id": run_id}
            return [
                (run_id, "1" * 64, {"analysis_contract_id": "contract:1"}),
                (run_id, "2" * 64, {"analysis_contract_id": "contract:2"}),
            ]

    resolver = system_test._runtime_authority_resolver_for_store(Store())

    with pytest.raises(
        ValueError,
        match="^runtime_authority_contract_ambiguous$",
    ):
        resolver(run_id)


def test_real_eval_prefers_persistent_runtime_evidence_resolver_from_store():
    from tools.phase7 import run_live_conversation_system_test as system_test

    process_local_query_resolver = object()
    persistent_runtime_resolver = object()

    class Store:
        def runtime_evidence_resolver(self):
            return persistent_runtime_resolver

    assert system_test._runtime_evidence_resolver_for_store(
        Store(),
        fallback=process_local_query_resolver,
    ) is persistent_runtime_resolver


def test_dry_eval_uses_process_local_resolver_without_store_factory():
    from tools.phase7 import run_live_conversation_system_test as system_test

    process_local_query_resolver = object()

    assert system_test._runtime_evidence_resolver_for_store(
        object(),
        fallback=process_local_query_resolver,
        required=False,
    ) is process_local_query_resolver


@pytest.mark.parametrize("factory_behavior", ["missing", "none", "error"])
def test_real_eval_fails_closed_without_persistent_runtime_evidence_resolver(
    factory_behavior,
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    class Store:
        if factory_behavior != "missing":
            def runtime_evidence_resolver(self):
                if factory_behavior == "error":
                    raise RuntimeError("database unavailable")
                return None

    with pytest.raises(
        RuntimeError,
        match="^eval_runtime_evidence_authority_unavailable$",
    ):
        system_test._runtime_evidence_resolver_for_store(
            Store(),
            fallback=object(),
            required=True,
        )


@pytest.mark.parametrize("path_kind", ["absolute_outside", "traversal", "symlink_escape"])
def test_runtime_audit_package_rejects_artifact_path_escape(
    tmp_path, monkeypatch, path_kind
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    monkeypatch.setattr(system_test, "ROOT", tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    if path_kind == "absolute_outside":
        path = str(outside)
    elif path_kind == "traversal":
        path = "artifacts/../outside.json"
    else:
        link = artifact_root / "escape.json"
        link.symlink_to(outside)
        path = str(link)

    assert system_test._runtime_audit_package({
        "run_id": "run-expected",
        "artifact_path": path,
    }) == {"_authority_error": "artifact_path_outside_root"}


@pytest.mark.parametrize(
    "authority",
    [
        {"contract_gaps": [_canonical_gap().to_dict()]},
        {
            "analysis_contract": {
                **_analysis_contract(_canonical_gap()),
                "contract_gaps": [{
                    **_canonical_gap().to_dict(),
                    "gap_type": "invented_gap_type",
                }],
            }
        },
        *(
            {
                "analysis_contract": {
                    **_analysis_contract(_canonical_gap()),
                    "scope": {
                        "requested_metric_ids": ["paid_amount"],
                        "requested_dimension_ids": ["channel"],
                    },
                    "contract_gaps": [{
                        **_canonical_gap().to_dict(),
                        "gap_type": gap_type,
                        "gap_id": gap_id,
                    }],
                }
            }
            for gap_type, gap_id in (
                ("contract_absent", "metric:paid_amount:extra:contract_absent"),
                (
                    "contract_absent",
                    "capability:answer_verify:query_shape::contract_absent",
                ),
                ("source_unbound", "dataset:paid_order_success:extra:source_unbound"),
                ("unsupported_grain", "dimension:channel:grain"),
                ("unsupported_grain", "dimension:channel:fake:grain"),
                (
                    "capability_metric_unsupported",
                    "metric:paid_amount:extra:capability_metric_family_unsupported",
                ),
            )
        ),
        {
            "analysis_contract": {
                **_analysis_contract(_canonical_gap()),
                "contract_gaps": [{
                    **_canonical_gap().to_dict(),
                    "gap_id": "fake:gap",
                }],
            }
        },
        {
            "analysis_contract": {
                **_analysis_contract(_canonical_gap()),
                "contract_gaps": [{
                    **_canonical_gap().to_dict(),
                    "gap_id": "capability:answer_verify:fake",
                }],
            }
        },
        {
            "analysis_contract": {
                **_analysis_contract(_canonical_gap()),
                "contract_gaps": [{
                    **_canonical_gap().to_dict(),
                    "gap_id": "capability:answer_verify:required_query",
                }],
            }
        },
        {
            "analysis_contract": {
                **_analysis_contract(_canonical_gap()),
                "scope": {
                    "requested_metric_ids": ["paid_amount"],
                    "requested_dimension_ids": [],
                },
                "contract_gaps": [{
                    **_canonical_gap().to_dict(),
                    "gap_id": "metric:paid_amount:missing",
                }],
            }
        },
        {
            "analysis_contract": {
                **_analysis_contract(_canonical_gap()),
                "contract_gaps": [{
                    **_canonical_gap().to_dict(),
                    "gap_type": "source_unbound",
                    "gap_id": "capability:answer_verify:contract_partial",
                }],
            }
        },
        {
            "analysis_contract": {
                **_analysis_contract(_canonical_gap()),
                "contract_gaps": [{
                    key: value
                    for key, value in _canonical_gap().to_dict().items()
                    if key != "repair_options"
                }],
            }
        },
    ],
)
def test_capability_block_requires_canonical_persisted_analysis_contract_gap(
    authority
):
    from tools.phase7.run_live_conversation_system_test import (
        _derive_capability_outcomes,
    )

    assert _derive_capability_outcomes(
        ("answer_verify",),
        accepted_capabilities=set(),
        authority=authority,
    ) == {"answer_verify": "missing_route"}


def test_capability_block_accepts_canonical_exact_analysis_contract_gap():
    from tools.phase7.run_live_conversation_system_test import (
        _derive_capability_outcomes,
    )

    assert _derive_capability_outcomes(
        ("answer_verify",),
        accepted_capabilities=set(),
        authority={"analysis_contract": _analysis_contract(_canonical_gap())},
    ) == {"answer_verify": "blocked"}


def test_queryless_block_requires_compiler_persisted_exact_gap():
    from bi_agent.runtime.analysis_contract_compiler import (
        compile_analysis_contract,
    )
    from tools.phase7.run_live_conversation_system_test import (
        _derive_capability_outcomes,
    )

    registry = RuntimeContractRegistry.from_path(
        CANONICAL_RUNTIME_BINDINGS_PATH
    )
    outcome = compile_analysis_contract(
        run_id="run-queryless-block-authority",
        proposal={
            "question_families": ["pattern_explanation"],
            "target_metrics": ["paid_amount"],
            "baselines": ["previous_day"],
            "claim_intents": ["recurring_pattern_existence"],
        },
        accepted_capabilities=(
            "metric_timeseries",
            "evidence_reduce",
            "answer_verify",
        ),
        catalog=DatasetCatalog(()),
        registry=registry,
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        permission_scope="analyst",
    )
    authority = {
        "analysis_contract": outcome.analysis_contract.to_dict(),
        "checkpoint_events": [
            {"node": "reduce_evidence", "status": "blocked"},
            {"node": "verify_answer", "status": "blocked"},
        ],
    }

    assert _derive_capability_outcomes(
        ("metric_timeseries", "evidence_reduce", "answer_verify"),
        accepted_capabilities=set(),
        authority=authority,
        registry=registry,
    ) == {
        "metric_timeseries": "blocked",
        "evidence_reduce": "blocked",
        "answer_verify": "missing_route",
    }


def test_capability_block_accepts_compiler_dimension_gap_without_binding():
    from tools.phase7.run_live_conversation_system_test import (
        _derive_capability_outcomes,
    )

    gap = ContractGap(
        gap_type="contract_absent",
        gap_id="dimension:unbound_dimension:contract_absent",
        dataset_id="",
        affected_capabilities=("answer_verify",),
        affected_claim_types=(),
        owner="contract_owner",
        repair_options=("register_dimension_contract",),
        requires_clarification=False,
        diagnostic_context={},
    )
    analysis_contract = _analysis_contract(gap)
    analysis_contract["scope"] = {
        "requested_metric_ids": [],
        "requested_dimension_ids": ["unbound_dimension"],
    }
    assert _derive_capability_outcomes(
        ("answer_verify",),
        accepted_capabilities=set(),
        authority={"analysis_contract": analysis_contract},
    ) == {"answer_verify": "blocked"}


def test_compiler_scope_persists_requested_metric_and_dimension_identities():
    from bi_agent.runtime.analysis_contract_compiler import _scope

    assert _scope({
        "scope": "full_sample",
        "target_metrics": ["paid_amount"],
        "requested_dimensions": ["unbound_dimension"],
    }, requested_metric_ids=("paid_amount", "paid_users"),
       requested_dimension_ids=("unbound_dimension",)) == {
        "type": "full_sample",
        "requested_metric_ids": ("paid_amount", "paid_users"),
        "requested_dimension_ids": ("unbound_dimension",),
    }


def test_capability_block_accepts_canonical_source_override_gap():
    from tools.phase7.run_live_conversation_system_test import (
        _derive_capability_outcomes,
    )

    gap = ContractGap(
        gap_type="contract_absent",
        gap_id="metric:paid_amount:source_unavailable:unknown_source",
        dataset_id="",
        affected_capabilities=("answer_verify",),
        affected_claim_types=(),
        owner="contract_owner",
        repair_options=("select_registered_source",),
        requires_clarification=False,
        diagnostic_context={},
    )
    contract = _analysis_contract(gap)
    contract["scope"] = {
        "requested_metric_ids": ["paid_amount"],
        "requested_dimension_ids": [],
    }
    assert _derive_capability_outcomes(
        ("answer_verify",),
        accepted_capabilities=set(),
        authority={"analysis_contract": contract},
    ) == {"answer_verify": "blocked"}


def test_capability_outcome_executes_from_persisted_plan_query_result_chain():
    from tools.phase7.run_live_conversation_system_test import (
        _derive_capability_outcomes,
    )

    authority, registry = _persisted_plan_authority()
    assert _derive_capability_outcomes(
        ("market_health_compare",),
        accepted_capabilities={"market_health_compare"},
        authority=authority,
        registry=registry,
    ) == {"market_health_compare": "executed"}


def test_hard_acceptance_outcomes_preserve_choice_scoped_block_and_executed_path():
    from tools.phase7.run_live_conversation_system_test import (
        _derive_capability_outcomes,
    )

    authority, registry = _persisted_plan_authority()
    blocked = ContractGap(
        gap_type="contract_partial",
        gap_id=(
            "capability:data_quality_profile:required_query:"
            "data_quality_probe:unbound"
        ),
        dataset_id="paid_order_success",
        affected_capabilities=("data_quality_profile", "analysis_contract"),
        affected_claim_types=(),
        owner="analysis_contract_owner",
        repair_options=("bind_required_query_contract",),
        requires_clarification=True,
        diagnostic_context={},
    )
    authority["analysis_contract"]["capability_requirements"] = [
        "market_health_compare",
        "data_quality_profile",
    ]
    authority["analysis_contract"]["contract_gaps"] = [blocked.to_dict()]

    outcomes = _derive_capability_outcomes(
        ("market_health_compare", "data_quality_profile"),
        accepted_capabilities={"market_health_compare"},
        authority=authority,
        registry=registry,
    )

    assert outcomes == {
        "market_health_compare": "executed",
        "data_quality_profile": "blocked",
    }
    assert all(
        state in {"executed", "degraded", "blocked"}
        for state in outcomes.values()
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "partial",
        "missing_result",
        "mismatched_query",
        "mismatched_result",
        "missing_completeness",
        "failed_assertion",
        "bad_query_signature",
        "bad_capability_signature",
        "malformed_result",
        "malformed_completeness",
        "wrong_query_intent",
        "wrong_metric",
        "wrong_windows",
        "wrong_result_fields",
        "wrong_assertion_identity",
        "stale_analysis_contract",
        "duplicate_result_ref",
        "capability_not_accepted",
        "wrong_result_schema_type",
        "wrong_result_windows_type",
        "wrong_result_grain_type",
        "wrong_result_snapshot_type",
        "missing_required_schema_field",
        "stale_result_windows",
        "stale_result_grain",
        "stale_result_snapshot",
        "unknown_assertion",
        "duplicate_assertion",
        "malformed_assertion",
        "malformed_nested_assertion_details",
        "stale_report_coverage",
    ],
)
def test_capability_outcome_rejects_incomplete_or_mismatched_plan_query_chain(
    mutation
):
    from tools.phase7.run_live_conversation_system_test import (
        _derive_capability_outcomes,
    )

    authority, registry = _persisted_plan_authority()
    if mutation == "partial":
        authority["completeness_reports"][0].update({
            "completeness_status": "partial",
            "analysis_readiness": "degraded",
        })
    elif mutation == "missing_result":
        authority["query_results"] = []
    elif mutation == "mismatched_query":
        authority["query_results"][0]["query_contract_ref"] = "query:other"
    elif mutation == "mismatched_result":
        authority["completeness_reports"][0]["result_ref"] = "result:other"
    elif mutation == "missing_completeness":
        authority["completeness_reports"] = []
    elif mutation == "failed_assertion":
        authority["completeness_reports"][0]["assertion_results"][0]["passed"] = False
        authority["completeness_reports"][0]["failure_reasons"] = ["execution_failed"]
    elif mutation == "bad_query_signature":
        authority["query_contracts"][0]["contract_signature"] = "bad"
    elif mutation == "bad_capability_signature":
        authority["capability_execution_plans"][0][
            "capability_contract_signature"
        ] = "bad"
    elif mutation == "malformed_result":
        authority["query_results"][0].pop("query_hash")
    elif mutation == "malformed_completeness":
        authority["completeness_reports"][0]["unexpected"] = True
    elif mutation == "wrong_query_intent":
        authority["query_contracts"][0]["query_intent"] = "different_family"
        authority["query_contracts"][0]["contract_signature"] = query_contract_signature(
            authority["query_contracts"][0]
        )
    elif mutation == "wrong_metric":
        authority["query_contracts"][0]["metric_bindings"][0]["metric_id"] = "paid_users"
        authority["query_contracts"][0]["contract_signature"] = query_contract_signature(
            authority["query_contracts"][0]
        )
    elif mutation == "wrong_windows":
        authority["query_contracts"][0]["result_shape"]["required_window_ids"] = []
        authority["query_contracts"][0]["contract_signature"] = query_contract_signature(
            authority["query_contracts"][0]
        )
    elif mutation == "wrong_result_fields":
        authority["query_contracts"][0]["result_shape"]["required_fields"] = [
            "window_id"
        ]
        authority["query_contracts"][0]["contract_signature"] = query_contract_signature(
            authority["query_contracts"][0]
        )
    elif mutation == "wrong_assertion_identity":
        authority["completeness_reports"][0]["assertion_results"][0][
            "assertion"
        ] = "unrelated_check"
    elif mutation == "stale_analysis_contract":
        authority["capability_execution_plans"][0][
            "analysis_contract_ref"
        ] = "analysis:stale"
        authority["query_contracts"][0]["analysis_contract_ref"] = "analysis:stale"
        authority["query_contracts"][0]["contract_signature"] = query_contract_signature(
            authority["query_contracts"][0]
        )
    elif mutation == "duplicate_result_ref":
        authority["query_results"].append({
            **authority["query_results"][0],
            "query_contract_ref": "query:other",
            "completeness_report_ref": "completeness:other",
        })
    elif mutation == "capability_not_accepted":
        authority["analysis_contract"]["capability_requirements"] = []
    elif mutation == "wrong_result_schema_type":
        authority["query_results"][0]["observed_schema"] = ["window_id"]
    elif mutation == "wrong_result_windows_type":
        authority["query_results"][0]["observed_windows"] = "target_day"
    elif mutation == "wrong_result_grain_type":
        authority["query_results"][0]["observed_grain"] = [1]
    elif mutation == "wrong_result_snapshot_type":
        authority["query_results"][0]["source_snapshot_refs"] = [1]
    elif mutation == "missing_required_schema_field":
        authority["query_results"][0]["observed_schema"].pop("paid_amount")
    elif mutation == "stale_result_windows":
        authority["query_results"][0]["observed_windows"] = ["previous_day"]
    elif mutation == "stale_result_grain":
        authority["query_results"][0]["observed_grain"] = ["country"]
    elif mutation == "stale_result_snapshot":
        authority["query_results"][0]["source_snapshot_refs"] = ["snapshot:stale"]
    elif mutation == "unknown_assertion":
        authority["completeness_reports"][0]["assertion_results"].append({
            "assertion": "client_says_ok",
            "passed": True,
            "failure_reasons": [],
            "details": {},
        })
    elif mutation == "duplicate_assertion":
        authority["completeness_reports"][0]["assertion_results"].append({
            **authority["completeness_reports"][0]["assertion_results"][0]
        })
    elif mutation == "malformed_assertion":
        authority["completeness_reports"][0]["assertion_results"][0]["passed"] = 1
    elif mutation == "malformed_nested_assertion_details":
        authority["completeness_reports"][0]["assertion_results"][0]["details"] = {
            "nested": {1: "client-key"}
        }
    elif mutation == "stale_report_coverage":
        authority["completeness_reports"][0]["coverage_summary"]["rows_ref"] = (
            "rows:stale"
        )

    assert _derive_capability_outcomes(
        ("market_health_compare",),
        accepted_capabilities={"market_health_compare"},
        authority=authority,
        registry=registry,
    ) == {"market_health_compare": "unobserved"}


def test_capability_state_gate_ignores_unrelated_dataset_partial_collapse():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    authority, registry = _persisted_plan_authority()
    gap = ContractGap(
        gap_type="contract_partial",
        gap_id="capability:source_reconciliation:required_query:probe:unbound",
        dataset_id="market_dashboard",
        affected_capabilities=("source_reconciliation",),
        affected_claim_types=(),
        owner="contract_owner",
        repair_options=("bind_required_query_contract",),
        requires_clarification=False,
        diagnostic_context={},
    )
    contract = AnalysisContract(
        **{
            **analysis_contract_from_dict(authority["analysis_contract"]).__dict__,
            "question_families": ("revenue_health_review",),
            "capability_requirements": (
                "market_health_compare",
                "source_reconciliation",
                "data_quality_profile",
                "formula_decompose",
            ),
            "contract_gaps": (
                gap,
                *(
                    ContractGap(
                        gap_type="contract_partial",
                        gap_id=(
                            f"capability:{capability_id}:required_query:probe:unbound"
                        ),
                        dataset_id="market_dashboard",
                        affected_capabilities=(capability_id,),
                        affected_claim_types=(),
                        owner="contract_owner",
                        repair_options=("bind_required_query_contract",),
                        requires_clarification=False,
                        diagnostic_context={},
                    )
                    for capability_id in (
                        "data_quality_profile",
                        "formula_decompose",
                    )
                ),
            ),
        }
    )
    _bind_run_matched_accepted_contract(authority, contract)
    turn = {
        "status": "completed",
        "answer_package": {"summary": "terminal"},
        "accepted_graph": ["market_health_compare"],
        "scenario": {
            "question_family": "revenue_health_review",
            "required_capabilities": [
                "market_health_compare",
                "source_reconciliation",
            ],
            "expected_capability_states": {
                "market_health_compare": "executable",
            },
            "expected_dataset_states": {"market_dashboard": "executable"},
            "allowed_claim_ceiling": "trust_boundary",
            "terminal_boundary": "verified_answer",
        },
        "runtime_authority": authority,
    }
    coverage_authority = {
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
    }

    review = review_case_obligations(
        turn, registry, coverage_authority=coverage_authority
    )

    assert review["capability_outcomes"] == {
        "data_quality_profile": "blocked",
        "formula_decompose": "blocked",
        "market_health_compare": "executed",
    }
    assert review["authored_required_capability_mismatches"] == [
        "source_reconciliation"
    ]
    assert review["capability_state_mismatches"] == []
    assert review["terminal_boundary"] == "verified_answer"
    assert review["terminal_outcome"] == "verified_answer"
    assert review["observed_dataset_states"] == {
        "market_dashboard": "contract_partial"
    }
    assert review["expected_dataset_states"] == {
        "market_dashboard": "executable"
    }
    assert review["missing_current_data_obligations"] == [
        "market_dashboard:executable"
    ]
    assert review["observed_capability_dataset_states"] == {
        "market_health_compare": [{
            "cell_id": "market_health_compare:market_dashboard",
            "dataset_id": "market_dashboard",
            "authority_state": "executable",
            "outcome": "executed",
            "observed_state": "executable",
        }]
    }
    assert review["dataset_obligation_gate_mode"] == "capability_authority"
    assert review["hard_acceptance_passed"] is True


def test_capability_outcome_rejects_generic_execution_binding_fallback():
    from tools.phase7.run_live_conversation_system_test import (
        _derive_capability_outcomes,
    )

    authority, registry = _persisted_plan_authority()
    authority["capability_execution_plans"] = []
    authority["capability_bindings"] = [{
        "capability_id": "market_health_compare",
        "status": "ready",
        "result_refs": ["result:plan-authority"],
    }]
    authority["query_executions"] = [{
        "result_ref": "result:plan-authority",
        "execution_status": "succeeded",
        "completeness_status": "complete",
        "analysis_readiness": "ready",
    }]

    assert _derive_capability_outcomes(
        ("market_health_compare",),
        accepted_capabilities={"market_health_compare"},
        authority=authority,
        registry=registry,
    ) == {"market_health_compare": "unobserved"}
