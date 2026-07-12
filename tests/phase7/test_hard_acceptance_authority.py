from __future__ import annotations

import json

import pytest

from bi_agent.runtime.analysis_contracts import (
    AnalysisContract,
    ContractGap,
    query_contract_signature,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


def _analysis_contract(*gaps: ContractGap) -> dict[str, object]:
    return AnalysisContract(
        analysis_contract_id="analysis-contract:test",
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


@pytest.mark.parametrize(
    "artifact_state",
    [
        "missing",
        "corrupt",
        "run_mismatch",
        "missing_expected_run_id",
        "missing_payload_run_id",
    ],
)
def test_runtime_audit_package_never_falls_back_to_client_gap_authority(
    tmp_path, artifact_state
):
    from tools.phase7.run_live_conversation_system_test import _runtime_audit_package

    path = tmp_path / "answer_package.json"
    if artifact_state == "corrupt":
        path.write_text("{not-json", encoding="utf-8")
    elif artifact_state == "run_mismatch":
        path.write_text(json.dumps({"run_id": "run-other"}), encoding="utf-8")
    elif artifact_state == "missing_expected_run_id":
        path.write_text(json.dumps({"run_id": "run-artifact"}), encoding="utf-8")
    elif artifact_state == "missing_payload_run_id":
        path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    result = {
        "artifact_path": str(path),
        "answer_package": {
            "admin_audit": {
                "analysis_contract": _analysis_contract(_canonical_gap())
            },
        },
    }
    if artifact_state != "missing_expected_run_id":
        result["run_id"] = "run-expected"
        result["answer_package"]["run_id"] = "run-expected"

    assert _runtime_audit_package(result) == {}


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
