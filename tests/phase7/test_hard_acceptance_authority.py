from __future__ import annotations

import json

import pytest

from bi_agent.runtime.analysis_contracts import AnalysisContract, ContractGap


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
