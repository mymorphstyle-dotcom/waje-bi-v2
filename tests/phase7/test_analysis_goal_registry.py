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
LAUNCH_GOAL_QUESTION_FAMILIES = {
    "explain_change": "paid_amount_change_explanation",
    "pattern_explanation": "pattern_explanation",
    "business_object_impact_review": "business_object_impact_review",
    "revenue_health_review": "revenue_health_review",
    "segment_or_factor_attribution": "segment_or_factor_attribution",
    "anomaly_or_black_swan_review": "anomaly_or_black_swan_review",
    "custom_baseline_comparison": "custom_baseline_comparison",
    "data_quality_or_evidence_review": "data_quality_or_evidence_review",
}


def _payload() -> dict:
    return load_contract(CANONICAL_RUNTIME_BINDINGS_PATH)


def test_explain_change_plan_exposes_full_deterministic_axis_universe() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    plan = registry.compile_goal_analysis_plan(
        goal_bindings=EXPLAIN_CHANGE,
        target_metric="paid_amount",
        explicit_focus=EMPTY_FOCUS,
    )

    assert registry.analysis_goal_ids == tuple(LAUNCH_GOAL_QUESTION_FAMILIES)
    assert registry.analysis_axis_ids == (
        "change_validation",
        "formula_tree",
        "dimension_localization",
        "time_context",
        "cross_source_context",
        "market_context",
        "business_context",
        "data_quality",
        "evidence_synthesis",
        "anomaly_validation",
        "metric_coverage",
    )
    assert registry.analysis_goal_semantics["explain_change"] == (
        "先验证目标指标相对主基线的真实方向和幅度，"
        "再从公式、聚合维度和时间背景定位主要驱动，"
        "并明确证据边界。"
    )
    assert plan["schema_version"] == "analysis_goal_plan.v2"
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
        ("market_context", "auxiliary"),
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
        "compare_period_phases",
        "weekday_calendar_compare",
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
        "formula_decompose",
        "candidate_dimension_screen",
        "segment_contribution",
        "segment_breakdown",
        "segment_shift_compare",
        "joint_attribution",
        "user_mix_contribution",
        "high_value_user_contribution",
        "metric_timeseries",
        "rolling_window_compare",
        "compare_period_phases",
        "weekday_calendar_compare",
        "cross_source_association",
        "cross_source_panel_association",
        "market_health_compare",
        "market_channel_context",
        "source_reconciliation",
        "data_quality_profile",
    }.issubset(all_capabilities)
    assert "answer_verify" not in all_capabilities
    assert "IP" not in str(plan)
    assert "device_id" not in str(plan)


def test_claim_strength_uses_global_policy_with_active_scope_validation() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    assert (
        registry.claim_required_publication_strength(
            "comparative_change",
            goal_ids=("explain_change",),
        )
        == "directional"
    )
    assert (
        registry.claim_required_publication_strength(
            "external_shock_candidate_or_anomaly",
            goal_ids=("explain_change",),
            axis_ids=("anomaly_validation",),
        )
        == "anomaly_candidate"
    )
    with pytest.raises(KeyError, match="active_claim_publication_strength_missing"):
        registry.claim_required_publication_strength(
            "external_shock_candidate_or_anomaly",
            goal_ids=("explain_change",),
            axis_ids=("business_context",),
        )
    assert (
        registry.claim_required_publication_strength(
            "candidate_mechanism",
            goal_ids=("explain_change",),
            axis_ids=("business_context",),
        )
        == "candidate_mechanism"
    )


def test_every_advertised_axis_claim_resolves_to_global_requirement() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    for axis_id in registry.analysis_axis_ids:
        axis = registry.analysis_axis(axis_id)
        claim_kinds = {
            str(claim_kind)
            for capability_id in axis["capability_refs"]
            for capability in (registry.capability_inputs(capability_id),)
            if not capability.get("completion_authority")
            for claim_kind in capability.get("supported_claim_types", ())
        }
        for claim_kind in claim_kinds:
            assert (
                registry.claim_required_publication_strength(
                    claim_kind,
                    goal_ids=(),
                    axis_ids=(axis_id,),
                )
                == registry.claim_publication_requirements[claim_kind]
            )


def test_registry_rejects_capability_claim_ceiling_class_mismatch() -> None:
    payload = _payload()
    payload["capability_inputs"]["user_mix_contribution"][
        "supported_evidence_types"
    ] = ["observed_comparison"]

    with pytest.raises(
        ValueError,
        match="runtime_capability_publication_compatibility_invalid",
    ):
        RuntimeContractRegistry(payload)


def test_launch_goal_registry_covers_question_families() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    for goal_id, family in LAUNCH_GOAL_QUESTION_FAMILIES.items():
        assert registry.analysis_goal_question_family_ref(goal_id) == family
    assert {
        registry.analysis_goal_question_family_ref(goal_id)
        for goal_id in registry.analysis_goal_ids
    } == set(registry.launch_question_family_ids)


def test_each_launch_goal_declares_publishable_planning_boundaries() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    for goal_id in registry.analysis_goal_ids:
        contract = registry.analysis_goal_obligation(goal_id)
        claim_kinds = {
            claim_kind
            for outcome_claim_types in contract["outcome_claim_types"].values()
            for claim_kind in outcome_claim_types
        }
        assert claim_kinds <= set(registry.claim_publication_requirements)
        assert {
            claim_kind: registry.claim_required_publication_strength(
                claim_kind,
                goal_ids=(goal_id,),
            )
            for claim_kind in claim_kinds
        } == {
            claim_kind: registry.claim_publication_requirements[claim_kind]
            for claim_kind in claim_kinds
        }
        assert contract["completion_policy"] == {
            "obligation_success": "verified_or_explicit_boundary",
            "required_axis_completion": ("all_required_and_disclosure_axes_terminal"),
            "publication_authority": "verifier_passed",
        }
        assert contract["analysis_axes"]
        assert any(
            binding["role"] in {"required", "disclosure"}
            for binding in contract["analysis_axes"]
        )


def test_analysis_axes_cover_every_public_capability() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    axis_capabilities = {
        capability_id
        for axis_id in registry.analysis_axis_ids
        for capability_id in registry.analysis_axis(axis_id)["capability_refs"]
    }

    assert axis_capabilities == set(registry.public_capability_ids)


def test_goal_mandatory_axis_capabilities_bind_to_required_claims() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    for goal_id in registry.analysis_goal_ids:
        goal = registry.analysis_goal_obligation(goal_id)
        goal_claim_kinds = {
            claim_kind
            for claim_kinds in goal["outcome_claim_types"].values()
            for claim_kind in claim_kinds
        }
        required_axis_capabilities = {
            capability_id
            for binding in goal["analysis_axes"]
            if binding["role"] in {"required", "disclosure"}
            for capability_id in registry.analysis_axis(binding["axis_id"])[
                "capability_refs"
            ]
        }

        for capability_id in required_axis_capabilities:
            capability = registry.capability_inputs(capability_id)
            assert capability.get(
                "completion_authority"
            ) == "verifier_passed" or goal_claim_kinds & set(
                capability.get("supported_claim_types") or ()
            )


def test_compiled_goal_plan_carries_contract_owned_family_and_boundaries() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    for goal_id, question_family_ref in LAUNCH_GOAL_QUESTION_FAMILIES.items():
        plan = registry.compile_goal_analysis_plan(
            goal_bindings=[{"goal_id": goal_id, "role": "primary"}],
            target_metric="paid_amount",
            explicit_focus=EMPTY_FOCUS,
        )

        assert plan["question_family_refs"] == [question_family_ref]
        goal_claim_kinds = {
            claim_kind
            for claim_types in registry.analysis_goal_obligation(goal_id)[
                "outcome_claim_types"
            ].values()
            for claim_kind in claim_types
        }
        assert plan["goal_claim_publication_requirements"] == {
            goal_id: {
                claim_kind: registry.claim_publication_requirements[claim_kind]
                for claim_kind in sorted(goal_claim_kinds)
            }
        }
        assert plan["goal_completion_policies"] == {
            goal_id: registry.analysis_goal_obligation(goal_id)["completion_policy"]
        }


def test_registry_requires_full_goal_family_coverage_and_allows_multiple_goals() -> (
    None
):
    payload = _payload()
    missing = deepcopy(payload)
    del missing["goal_obligations"]["pattern_explanation"]
    with pytest.raises(ValueError, match="runtime_analysis_goal_family_coverage"):
        RuntimeContractRegistry(missing)

    multiple = deepcopy(payload)
    multiple["goal_obligations"]["explain_change_diagnostic"] = deepcopy(
        multiple["goal_obligations"]["explain_change"]
    )
    registry = RuntimeContractRegistry(multiple)
    assert (
        registry.analysis_goal_question_family_ref("explain_change_diagnostic")
        == "paid_amount_change_explanation"
    )


def test_registry_rejects_claim_publication_or_completion_policy_drift() -> None:
    payload = _payload()
    stronger_reachable_requirement = deepcopy(payload)
    stronger_reachable_requirement["claim_publication_policy"][
        "minimum_strength_by_claim_kind"
    ]["baseline_stability"] = "recurring_pattern"
    RuntimeContractRegistry(stronger_reachable_requirement)

    missing_claim_requirement = deepcopy(payload)
    del missing_claim_requirement["claim_publication_policy"][
        "minimum_strength_by_claim_kind"
    ]["comparative_change"]
    with pytest.raises(
        ValueError,
        match="runtime_claim_publication_requirement_coverage",
    ):
        RuntimeContractRegistry(missing_claim_requirement)

    claim_class_mismatch = deepcopy(payload)
    claim_class_mismatch["claim_publication_policy"]["minimum_strength_by_claim_kind"][
        "business_object_candidate_impact"
    ] = "candidate_mechanism"
    with pytest.raises(
        ValueError,
        match="runtime_claim_composite_support_policy_invalid",
    ):
        RuntimeContractRegistry(claim_class_mismatch)

    unknown_strength = deepcopy(payload)
    unknown_strength["claim_publication_policy"]["minimum_strength_by_claim_kind"][
        "comparative_change"
    ] = "invented_strength"
    with pytest.raises(
        ValueError,
        match="runtime_claim_publication_requirement_invalid",
    ):
        RuntimeContractRegistry(unknown_strength)

    invalid_completion = deepcopy(payload)
    invalid_completion["goal_obligations"]["explain_change"]["completion_policy"][
        "publication_authority"
    ] = "planner_approved"
    with pytest.raises(
        ValueError,
        match="runtime_analysis_goal_completion_policy_invalid",
    ):
        RuntimeContractRegistry(invalid_completion)


def test_public_capabilities_are_the_current_axis_projection() -> None:
    payload = _payload()
    missing_axis_capability = deepcopy(payload)
    missing_axis_capability["analysis_axis_catalog"]["dimension_localization"][
        "capability_refs"
    ].remove("high_value_user_contribution")
    registry = RuntimeContractRegistry(missing_axis_capability)

    assert "high_value_user_contribution" in registry.capability_ids
    assert "high_value_user_contribution" not in registry.public_capability_ids
    assert registry.public_capability_ids == tuple(
        capability_id
        for capability_id in registry.capability_ids
        if any(
            capability_id in axis["capability_refs"]
            for axis in missing_axis_capability["analysis_axis_catalog"].values()
        )
    )


def test_registry_rejects_mandatory_capability_without_required_claim() -> None:
    payload = _payload()
    unbound_required_capability = deepcopy(payload)
    unbound_required_capability["capability_inputs"]["compare_periods"][
        "supported_claim_types"
    ] = ["recurring_pattern_existence"]
    with pytest.raises(
        ValueError,
        match="runtime_analysis_goal_required_capability_unbound",
    ):
        RuntimeContractRegistry(unbound_required_capability)


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
    assert by_id["dimension_localization"]["explicit_focus_refs"]["dimension_ids"] == [
        "region"
    ]
    assert by_id["business_context"]["explicit_focus_refs"]["context_source_ids"] == [
        "external_event"
    ]
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
    payload["analysis_axis_catalog"]["dimension_localization"]["dimension_refs"].remove(
        "device_model"
    )

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
    missing_axis["analysis_axis_catalog"]["unbound_change_validation"] = deepcopy(
        missing_axis["analysis_axis_catalog"]["change_validation"]
    )
    missing_axis["analysis_axis_catalog"]["unbound_change_validation"][
        "reconciliation_group"
    ] = "paid_amount_unbound_change_validation"
    with pytest.raises(ValueError, match="runtime_analysis_goal_axis_coverage"):
        RuntimeContractRegistry(missing_axis)

    unsupported_claim = deepcopy(payload)
    unsupported_claim["goal_obligations"]["explain_change"]["outcome_claim_types"][
        "direction_and_magnitude"
    ] = ["invented_claim_type"]
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
