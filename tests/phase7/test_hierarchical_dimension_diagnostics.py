from bi_agent.capabilities import candidate_dimension_screen


def _profile(payload, dimension):
    return next(
        item for item in payload["dimension_profiles"] if item["dimension"] == dimension
    )


def test_constant_parent_is_coverage_only_and_informative_descendants_form_readout_path():
    evidence = candidate_dimension_screen(
        {
            "market": (
                {
                    "market": "M1",
                    "group": "baseline",
                    "amount": 100,
                    "paid_orders": 10,
                    "paid_users": 10,
                    "n": 100,
                },
                {
                    "market": "M1",
                    "group": "target",
                    "amount": 120,
                    "paid_orders": 10,
                    "paid_users": 10,
                    "n": 100,
                },
            ),
            "area": (
                {
                    "area": "A1",
                    "group": "baseline",
                    "amount": 70,
                    "paid_orders": 7,
                    "paid_users": 7,
                    "n": 70,
                },
                {
                    "area": "A2",
                    "group": "baseline",
                    "amount": 30,
                    "paid_orders": 3,
                    "paid_users": 3,
                    "n": 30,
                },
                {
                    "area": "A1",
                    "group": "target",
                    "amount": 100,
                    "paid_orders": 8,
                    "paid_users": 8,
                    "n": 80,
                },
                {
                    "area": "A2",
                    "group": "target",
                    "amount": 20,
                    "paid_orders": 2,
                    "paid_users": 2,
                    "n": 20,
                },
            ),
            "locality": (
                {
                    "locality": "L1",
                    "group": "baseline",
                    "amount": 40,
                    "paid_orders": 4,
                    "paid_users": 4,
                    "n": 40,
                },
                {
                    "locality": "L2",
                    "group": "baseline",
                    "amount": 30,
                    "paid_orders": 3,
                    "paid_users": 3,
                    "n": 30,
                },
                {
                    "locality": "L3",
                    "group": "baseline",
                    "amount": 20,
                    "paid_orders": 2,
                    "paid_users": 2,
                    "n": 20,
                },
                {
                    "locality": "L4",
                    "group": "baseline",
                    "amount": 10,
                    "paid_orders": 1,
                    "paid_users": 1,
                    "n": 10,
                },
                {
                    "locality": "L1",
                    "group": "target",
                    "amount": 70,
                    "paid_orders": 5,
                    "paid_users": 5,
                    "n": 50,
                },
                {
                    "locality": "L2",
                    "group": "target",
                    "amount": 30,
                    "paid_orders": 3,
                    "paid_users": 3,
                    "n": 30,
                },
                {
                    "locality": "L3",
                    "group": "target",
                    "amount": 10,
                    "paid_orders": 1,
                    "paid_users": 1,
                    "n": 10,
                },
                {
                    "locality": "L4",
                    "group": "target",
                    "amount": 10,
                    "paid_orders": 1,
                    "paid_users": 1,
                    "n": 10,
                },
            ),
        },
        overall_by_group={"baseline": 100, "target": 120},
        complete_dimensions=("market", "area", "locality"),
        dimension_labels={
            "market": "市场",
            "area": "地区",
            "locality": "城市",
        },
        dimension_metadata={
            "market": {
                "hierarchy_id": "location",
                "hierarchy_level": "market",
            },
            "area": {
                "hierarchy_id": "location",
                "hierarchy_level": "area",
                "parent_dimension": "market",
            },
            "locality": {
                "hierarchy_id": "location",
                "hierarchy_level": "locality",
                "parent_dimension": "area",
            },
        },
        global_primary_factor="avg_order_amount",
        min_sample_size=10,
    )

    payload = evidence.typed_payload
    parent = _profile(payload, "market")
    area = _profile(payload, "area")
    locality = _profile(payload, "locality")

    assert parent["reconciliation_status"] == "passed"
    assert parent["candidate_eligible"] is True
    assert parent["business_readout_eligible"] is False
    assert parent["selection_status"] == "internal_coverage_only"
    assert parent["dimension_differentiation_score"] == 0.0
    assert parent["excess_movement"] == 0.0

    assert area["business_readout_eligible"] is True
    assert locality["business_readout_eligible"] is True
    assert area["excess_movement"] > 0
    assert locality["excess_movement"] > area["excess_movement"]
    assert area["primary_factor_alignment_coverage"] > 0
    assert locality["primary_factor_alignment_coverage"] > 0

    assert payload["coverage_ready_dimensions"] == (
        "market",
        "area",
        "locality",
    )
    assert payload["eligible_dimensions"] == ("locality", "area")
    assert payload["selected_dimension"] == "locality"
    assert payload["selected_hierarchy_id"] == "location"
    assert payload["selected_hierarchy_dimensions"] == ("area", "locality")
    assert tuple(
        item["dimension"] for item in payload["selected_business_readouts"]
    ) == ("area", "locality")
    assert tuple(item["dimension_id"] for item in payload["dimension_findings"]) == (
        "area",
        "locality",
    )
    assert evidence.numeric_facts["area_paid_amount_excess_delta"] == 16.0
    assert evidence.numeric_facts["locality_paid_amount_excess_delta"] == 22.0
    assert "地区→城市" in payload["business_readout"]

    hierarchy = payload["hierarchy_diagnostics"][0]
    assert hierarchy["hierarchy_id"] == "location"
    assert hierarchy["coverage_dimensions"] == ("market",)
    assert hierarchy["business_readout_dimensions"] == ("area", "locality")
    assert hierarchy["dimension_path"] == ("market", "area", "locality")
    assert payload["interpretation_contract"] == {
        "contract_id": "dimension-localization-interpretation.v1",
        "analysis_role": "auxiliary_localization",
        "ranking_scope": "cross_dimension_diagnostic_priority",
        "ranking_subject": "dimension_view",
        "ranking_measure": "diagnostic_priority_score",
        "ranking_order": "diagnostic_priority_score_descending",
        "ranking_position_measure": "priority_rank",
        "priority_rank_order": "ascending",
        "ranking_formula": ("excess_change_differentiation_primary_factor_alignment"),
        "cross_dimension_overlap": "overlapping_marginal_views",
        "cross_dimension_additivity": "forbidden",
        "cross_dimension_contribution_ranking": "forbidden",
        "within_dimension_additivity": {
            "scope": "complete_reconciled_partition",
            "additive_measures": (
                "baseline_amount",
                "target_amount",
                "delta",
            ),
            "zero_sum_measures": ("excess_delta",),
            "zero_sum_condition": "all_measure_values_defined",
            "non_additive_measures": (
                "dimension_differentiation_score",
                "diagnostic_priority_score",
            ),
        },
        "contribution_semantics": {
            "delta": "within_dimension_accounting_change",
            "excess_delta": "baseline_mix_structural_deviation",
            "diagnostic_priority_score": "cross_dimension_ranking_only",
        },
        "excess_delta_definition": (
            "target_amount_minus_target_total_at_baseline_share"
        ),
        "formula_decomposition_relationship": (
            "co_report_only_no_shared_rank_sum_or_share"
        ),
        "causal_interpretation": "forbidden",
        "representative_member_selection": ("separate_from_dimension_priority_ranking"),
        "score_explanation_contract": {
            "formula_id": "dimension-diagnostic-priority",
            "formula_version": "2",
            "formula": "sum(normalized_value * effective_weight)",
            "base_weights": {
                "excess_change_ratio": 0.45,
                "dimension_differentiation_score": 0.35,
                "primary_factor_alignment_score": 0.2,
            },
            "missing_component_policy": "renormalize_measured_component_weights",
            "subject_type": "dimension",
            "comparison_scope": "cross_dimension_diagnostic_priority",
        },
        "writer_fact_selection": {
            "mode": "named_fact_subset",
            "fact_names": (
                "dimension_count",
                "eligible_dimension_count",
                "dimension_label",
                "member",
                "baseline_amount",
                "target_amount",
                "delta",
                "excess_delta",
                "diagnostic_priority_score",
                "priority_rank",
                "primary_factor_alignment_coverage",
            ),
        },
    }
    priorities = {item["dimension"]: item for item in payload["diagnostic_priorities"]}
    for finding in payload["dimension_findings"]:
        priority = priorities[finding["dimension_id"]]
        assert finding["priority_rank"] == priority["priority_rank"]
        assert (
            finding["diagnostic_priority_score"]
            == priority["diagnostic_priority_score"]
        )
        assert "business_readout" not in finding


def test_near_constant_parent_does_not_outrank_material_child():
    evidence = candidate_dimension_screen(
        {
            "parent_level": (
                {
                    "parent_level": "P1",
                    "group": "baseline",
                    "amount": 990,
                    "n": 990,
                },
                {
                    "parent_level": "P2",
                    "group": "baseline",
                    "amount": 10,
                    "n": 10,
                },
                {
                    "parent_level": "P1",
                    "group": "target",
                    "amount": 1190,
                    "n": 1190,
                },
                {
                    "parent_level": "P2",
                    "group": "target",
                    "amount": 10,
                    "n": 10,
                },
            ),
            "child_level": (
                {
                    "child_level": "C1",
                    "group": "baseline",
                    "amount": 600,
                    "n": 600,
                },
                {
                    "child_level": "C2",
                    "group": "baseline",
                    "amount": 400,
                    "n": 400,
                },
                {
                    "child_level": "C1",
                    "group": "target",
                    "amount": 900,
                    "n": 900,
                },
                {
                    "child_level": "C2",
                    "group": "target",
                    "amount": 300,
                    "n": 300,
                },
            ),
        },
        overall_by_group={"baseline": 1000, "target": 1200},
        complete_dimensions=("parent_level", "child_level"),
        dimension_metadata={
            "parent_level": {
                "hierarchy_id": "generic_hierarchy",
                "hierarchy_level": "parent",
            },
            "child_level": {
                "hierarchy_id": "generic_hierarchy",
                "hierarchy_level": "child",
                "parent_dimension": "parent_level",
            },
        },
        min_sample_size=10,
    )

    payload = evidence.typed_payload
    parent = _profile(payload, "parent_level")
    child = _profile(payload, "child_level")

    assert parent["reconciliation_status"] == "passed"
    assert parent["near_constant_dimension"] is True
    assert parent["business_readout_eligible"] is False
    assert parent["selection_status"] == "internal_coverage_only"
    assert "near_constant_dimension:parent_level" in parent["limitations"]
    assert child["business_readout_eligible"] is True
    assert child["diagnostic_priority_score"] > parent["diagnostic_priority_score"]
    assert payload["selected_dimension"] == "child_level"
    assert payload["selected_hierarchy_dimensions"] == ("child_level",)


def test_all_informative_hierarchies_survive_global_priority_ranking():
    evidence = candidate_dimension_screen(
        {
            "region": (
                {"region": "R1", "group": "baseline", "amount": 70, "n": 70},
                {"region": "R2", "group": "baseline", "amount": 30, "n": 30},
                {"region": "R1", "group": "target", "amount": 100, "n": 100},
                {"region": "R2", "group": "target", "amount": 20, "n": 20},
            ),
            "device_brand": (
                {
                    "device_brand": "D1",
                    "group": "baseline",
                    "amount": 60,
                    "n": 60,
                },
                {
                    "device_brand": "D2",
                    "group": "baseline",
                    "amount": 40,
                    "n": 40,
                },
                {
                    "device_brand": "D1",
                    "group": "target",
                    "amount": 80,
                    "n": 80,
                },
                {
                    "device_brand": "D2",
                    "group": "target",
                    "amount": 40,
                    "n": 40,
                },
            ),
        },
        overall_by_group={"baseline": 100, "target": 120},
        complete_dimensions=("region", "device_brand"),
        dimension_labels={"region": "地区", "device_brand": "设备品牌"},
        dimension_metadata={
            "region": {"hierarchy_id": "geo", "hierarchy_level": "region"},
            "device_brand": {
                "hierarchy_id": "device_environment",
                "hierarchy_level": "brand",
            },
        },
        min_sample_size=10,
    )

    payload = evidence.typed_payload
    assert set(payload["eligible_dimensions"]) == {"region", "device_brand"}
    assert {item["dimension_id"] for item in payload["dimension_findings"]} == {
        "region",
        "device_brand",
    }
    assert "地区" in payload["business_readout"]
    assert "设备品牌" in payload["business_readout"]
