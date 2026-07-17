from copy import deepcopy

import pytest

from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


EMPTY_FOCUS = {
    "component_ids": [],
    "dimension_ids": [],
    "context_source_ids": [],
}
EXPLAIN_CHANGE = [{"goal_id": "explain_change", "role": "primary"}]


def _payload() -> dict:
    return load_contract(CANONICAL_RUNTIME_BINDINGS_PATH)


def test_explain_change_plan_exposes_full_deterministic_axis_universe() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    plan = registry.compile_goal_analysis_plan(
        goal_bindings=EXPLAIN_CHANGE,
        target_metric="paid_amount",
        explicit_focus=EMPTY_FOCUS,
    )

    assert registry.analysis_goal_ids == ("explain_change",)
    assert registry.analysis_axis_ids == (
        "change_validation",
        "formula_tree",
        "dimension_localization",
        "time_context",
        "cross_source_context",
        "business_context",
        "data_quality",
    )
    assert registry.analysis_goal_semantics == {
        "explain_change": (
            "先验证目标指标相对主基线的真实方向和幅度，"
            "再从公式、聚合维度和时间背景定位主要驱动，"
            "并明确证据边界。"
        )
    }
    assert plan["schema_version"] == "analysis_goal_plan.v1"
    assert plan["required_outcomes"] == [
        "direction_and_magnitude",
        "ranked_drivers",
        "quantified_contributions",
        "evidence_boundaries",
    ]
    assert plan["outcome_claim_types"] == {
        "direction_and_magnitude": ["comparative_change"],
        "ranked_drivers": [
            "formula_component_contribution",
            "segment_contribution_or_mix_shift",
        ],
        "quantified_contributions": [
            "formula_component_contribution",
            "segment_contribution_or_mix_shift",
        ],
        "evidence_boundaries": ["contract_coverage_and_trust_boundary"],
    }
    assert [(axis["axis_id"], axis["role"]) for axis in plan["analysis_axes"]] == [
        ("change_validation", "required"),
        ("formula_tree", "required"),
        ("dimension_localization", "auxiliary"),
        ("time_context", "auxiliary"),
        ("cross_source_context", "auxiliary"),
        ("business_context", "conditional"),
        ("data_quality", "disclosure"),
    ]
    by_id = {axis["axis_id"]: axis for axis in plan["analysis_axes"]}
    assert by_id["change_validation"]["capability_refs"] == ["compare_periods"]
    assert by_id["change_validation"]["reconciliation_group"] == (
        "paid_amount_primary_comparison"
    )
    assert by_id["dimension_localization"]["dimension_refs"] == [
        "channel",
        "payment_method",
        "country",
        "region",
        "city",
        "device_brand",
        "device_model",
        "os",
        "network_type",
    ]
    assert by_id["time_context"]["capability_refs"] == [
        "metric_timeseries",
        "rolling_window_compare",
    ]
    assert by_id["cross_source_context"]["capability_refs"] == [
        "cross_source_association",
        "cross_source_panel_association",
    ]
    assert by_id["cross_source_context"]["dimension_refs"] == ["channel"]
    all_capabilities = {
        capability
        for axis in plan["analysis_axes"]
        for capability in axis["capability_refs"]
    }
    assert {
        "compare_periods",
        "driver_decomposition",
        "formula_decompose",
        "candidate_dimension_screen",
        "metric_timeseries",
        "cross_source_association",
        "cross_source_panel_association",
        "data_quality_profile",
        "answer_verify",
    }.issubset(all_capabilities)
    assert "joint_attribution" not in all_capabilities
    assert "IP" not in str(plan)
    assert "device_id" not in str(plan)


def test_cross_source_capabilities_require_both_sides_of_the_association() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    for capability_id in (
        "cross_source_association",
        "cross_source_panel_association",
    ):
        contract = registry.capability_inputs(capability_id)
        assert contract["query_families"] == [
            "association_outcome_timeseries",
            "association_candidate_timeseries",
        ]
        assert contract.get("optional_query_families", []) == []


def test_explicit_focus_upgrades_matching_axes_without_narrowing_universe() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    plan = registry.compile_goal_analysis_plan(
        goal_bindings=EXPLAIN_CHANGE,
        target_metric="paid_amount",
        explicit_focus={
            "component_ids": ["payment_success_rate"],
            "dimension_ids": ["region"],
            "context_source_ids": ["external_event"],
        },
    )

    by_id = {axis["axis_id"]: axis for axis in plan["analysis_axes"]}
    assert by_id["formula_tree"]["role"] == "required"
    assert by_id["dimension_localization"]["role"] == "required"
    assert by_id["business_context"]["role"] == "required"
    assert by_id["formula_tree"]["explicit_focus_refs"]["component_ids"] == [
        "payment_success_rate"
    ]
    assert by_id["dimension_localization"]["explicit_focus_refs"][
        "dimension_ids"
    ] == ["region"]
    assert by_id["business_context"]["explicit_focus_refs"][
        "context_source_ids"
    ] == ["external_event"]
    assert by_id["dimension_localization"]["dimension_refs"] == [
        "channel",
        "payment_method",
        "country",
        "region",
        "city",
        "device_brand",
        "device_model",
        "os",
        "network_type",
    ]


def test_registry_requires_every_automatic_aggregate_dimension_in_axis() -> None:
    payload = _payload()
    payload["analysis_axis_catalog"]["dimension_localization"][
        "dimension_refs"
    ].remove("device_model")

    with pytest.raises(
        ValueError,
        match="runtime_analysis_axis_automatic_dimensions_mismatch",
    ):
        RuntimeContractRegistry(payload)


def test_registry_rejects_nonaggregate_automatic_dimension() -> None:
    payload = _payload()
    payload["dimensions"]["region"]["output_policy"] = "raw_values"

    with pytest.raises(
        ValueError,
        match="runtime_dimension_automatic_screening_contract_invalid:region",
    ):
        RuntimeContractRegistry(payload)


def test_registry_rejects_unknown_axis_members_and_capabilities() -> None:
    payload = _payload()
    unknown_member = deepcopy(payload)
    unknown_member["analysis_axis_catalog"]["formula_tree"]["metric_refs"].append(
        "invented_metric"
    )
    with pytest.raises(
        ValueError,
        match="runtime_analysis_axis_reference_unknown:formula_tree:metric_refs",
    ):
        RuntimeContractRegistry(unknown_member)

    unknown_capability = deepcopy(payload)
    unknown_capability["analysis_axis_catalog"]["formula_tree"][
        "capability_refs"
    ].append("invented_capability")
    with pytest.raises(
        ValueError,
        match="runtime_analysis_axis_reference_unknown:formula_tree:capability_refs",
    ):
        RuntimeContractRegistry(unknown_capability)


def test_registry_rejects_unbound_goal_axes_and_outcome_claim_types() -> None:
    payload = _payload()
    missing_axis = deepcopy(payload)
    missing_axis["goal_obligations"]["explain_change"]["analysis_axes"].pop(3)
    with pytest.raises(ValueError, match="runtime_analysis_goal_axis_coverage"):
        RuntimeContractRegistry(missing_axis)

    unsupported_claim = deepcopy(payload)
    unsupported_claim["goal_obligations"]["explain_change"][
        "outcome_claim_types"
    ]["direction_and_magnitude"] = ["invented_claim_type"]
    with pytest.raises(
        ValueError,
        match="runtime_analysis_goal_outcome_claim_type_unsupported",
    ):
        RuntimeContractRegistry(unsupported_claim)


def test_compile_goal_plan_rejects_focus_outside_goal_axis_contract() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    with pytest.raises(
        ValueError,
        match="analysis_goal_explicit_focus_unbound:dimension_ids:gameplay",
    ):
        registry.compile_goal_analysis_plan(
            goal_bindings=EXPLAIN_CHANGE,
            target_metric="paid_amount",
            explicit_focus={
                "component_ids": [],
                "dimension_ids": ["gameplay"],
                "context_source_ids": [],
            },
        )


def test_route_executes_reviewed_dimension_axis_with_candidate_screen() -> None:
    from bi_agent.runtime import langgraph_workflow as workflow

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    plan = registry.compile_goal_analysis_plan(
        goal_bindings=EXPLAIN_CHANGE,
        target_metric="paid_amount",
        explicit_focus=EMPTY_FOCUS,
    )
    axes = plan["analysis_axes"]
    dimension_ids = list(
        dict.fromkeys(
            dimension_id
            for axis in axes
            for dimension_id in axis["dimension_refs"]
        )
    )
    component_ids = list(
        dict.fromkeys(
            metric_id
            for axis in axes
            if axis["axis_id"] == "formula_tree"
            for metric_id in axis["metric_refs"]
            if metric_id != "paid_amount"
        )
    )
    claim_types = list(
        dict.fromkeys(
            claim_type
            for axis in axes
            for capability_id in axis["capability_refs"]
            for claim_type in registry.capability_inputs(capability_id).get(
                "supported_claim_types", ()
            )
        )
    )
    intent = {
        "question_family": "paid_amount_change_explanation",
        "question_families": ["paid_amount_change_explanation"],
        "target_metric": "paid_amount",
        "analysis_axes": axes,
        "analysis_axis_ids": [axis["axis_id"] for axis in axes],
        "dimension_ids": dimension_ids,
        "component_ids": component_ids,
        "required_outcomes": plan["required_outcomes"],
        "publishable_claim_types": claim_types,
        "baseline_candidates": ["previous_day"],
        "scope": "full_sample",
    }
    requirements = {
        "target_metrics": ["paid_amount"],
        "component_ids": component_ids,
        "dimension_ids": dimension_ids,
        "baselines": ["previous_day"],
        "context_sources": [],
        "dataset_requirements": [],
        "diagnostic_tags": [],
        "claim_types": claim_types,
        "required_outcomes": plan["required_outcomes"],
        "analysis_axis_ids": [axis["axis_id"] for axis in axes],
        "scope": "full_sample",
    }

    capabilities, route = workflow.reconcile_analysis_route(
        ("compare_periods",),
        {"analysis_requirements": requirements},
        intent,
        registry,
    )

    assert route["analysis_requirements"]["dimension_ids"] == dimension_ids
    assert "candidate_dimension_screen" in capabilities
    assert "segment_contribution" not in capabilities
    assert "joint_attribution" not in capabilities
    assert "event_evidence" not in capabilities
    assert "event_window_compare" not in capabilities
    assert not hasattr(workflow, "_attach_auxiliary_dimension_screen")


def test_disclosure_axis_executes_without_becoming_a_required_business_claim() -> None:
    from bi_agent.runtime import langgraph_workflow as workflow

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    intent = workflow._bind_compiled_analysis_plan(
        {
            "goal_bindings": EXPLAIN_CHANGE,
            "explicit_focus": EMPTY_FOCUS,
            "target_metric": "paid_amount",
        },
        registry,
    )

    assert "contract_coverage_and_trust_boundary" not in intent[
        "required_claim_types"
    ]
    assert {
        "comparative_change",
        "formula_component_contribution",
    }.issubset(intent["required_claim_types"])
    assert next(
        axis for axis in intent["analysis_axes"] if axis["axis_id"] == "data_quality"
    )["role"] == "disclosure"
