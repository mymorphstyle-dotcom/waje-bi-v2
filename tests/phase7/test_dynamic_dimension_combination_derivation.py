import pytest

from bi_agent.runtime.dimension_combination_derivation import (
    DimensionCombinationDerivationError,
    derive_dimension_combinations,
    validate_dimension_combination_policy,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


def _policy():
    registry = RuntimeContractRegistry.from_path(
        CANONICAL_RUNTIME_BINDINGS_PATH
    )
    return registry.capability_inputs("joint_attribution")[
        "dynamic_dimension_combination_policy"
    ]


def _payload():
    candidates = (
        ("channel", 1, 8),
        ("region", 2, 10),
        ("city", 3, 40),
        ("device_brand", 4, 12),
        ("device_model", 5, 300),
    )
    return {
        _policy()["candidate_field"]: tuple(
            {
                "dimension": dimension,
                "priority_rank": rank,
            }
            for dimension, rank, _ in candidates
        ),
        "dimension_profiles": tuple(
            {
                "dimension": dimension,
                "segment_count": segment_count,
            }
            for dimension, _, segment_count in candidates
        ),
    }


def _metadata():
    return {
        "channel": {"hierarchy_id": "acquisition"},
        "region": {
            "hierarchy_id": "geo",
            "parent_dimension": "country",
        },
        "city": {
            "hierarchy_id": "geo",
            "parent_dimension": "region",
        },
        "device_brand": {"hierarchy_id": "device"},
        "device_model": {
            "hierarchy_id": "device",
            "parent_dimension": "device_brand",
        },
        "country": {"hierarchy_id": "geo"},
    }


def test_dynamic_dimension_policy_is_dependency_and_cost_bounded():
    policy = validate_dimension_combination_policy(_policy())

    assert policy["source_dependency"] == "candidate_dimension_screen"
    assert policy["candidate_pool_limit"] == 5
    assert policy["depth_budgets"] == [
        {"depth": 2, "maximum_combinations": 4},
        {"depth": 3, "maximum_combinations": 1},
    ]
    assert policy["maximum_estimated_cells_per_query"] == 100000
    assert policy["maximum_estimated_cells_total"] == 150000


def test_dynamic_dimension_combinations_follow_rank_hierarchy_and_cell_budgets():
    derived = derive_dimension_combinations(
        _payload(),
        dimension_metadata=_metadata(),
        policy=_policy(),
    )

    selected = tuple(
        tuple(item["dimension_ids"])
        for item in derived["selected_combinations"]
    )
    assert ("region", "city") not in selected
    assert ("device_brand", "device_model") not in selected
    assert selected
    assert len(tuple(item for item in selected if len(item) == 2)) <= 4
    assert len(tuple(item for item in selected if len(item) == 3)) <= 1
    assert all(
        item["estimated_cells"]
        <= _policy()["maximum_estimated_cells_per_query"]
        for item in derived["selected_combinations"]
    )
    assert (
        derived["estimated_cells_total"]
        <= _policy()["maximum_estimated_cells_total"]
    )


def test_dynamic_dimension_combinations_reject_uncontracted_candidates():
    payload = _payload()
    candidate_field = _policy()["candidate_field"]
    payload[candidate_field] = (
        *payload[candidate_field],
        {"dimension": "unregistered", "priority_rank": 6},
    )

    with pytest.raises(
        DimensionCombinationDerivationError,
        match="dimension_combination_evidence_invalid",
    ):
        derive_dimension_combinations(
            payload,
            dimension_metadata=_metadata(),
            policy=_policy(),
        )
