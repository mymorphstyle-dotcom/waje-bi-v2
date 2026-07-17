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
    assert payload["cross_dimension_additivity_allowed"] is False
    assert payload["within_dimension_amount_contribution_additive"] is True


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
    assert {
        item["dimension_id"] for item in payload["dimension_findings"]
    } == {"region", "device_brand"}
    assert "地区" in payload["business_readout"]
    assert "设备品牌" in payload["business_readout"]
