from pathlib import Path

import yaml

from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


ROOT = Path(__file__).resolve().parents[2]


def _load(path: str):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def _record(records, key: str, value: str):
    return next(record for record in records if record[key] == value)


def test_geo_and_device_contracts_follow_the_current_paid_order_source():
    source = _load("contracts/sources/paid-order-detail.source.yaml")
    dimensions = _load("contracts/dimensions/dimensions.yaml")["dimensions"]

    source_fields = {
        field["name"] for field in source["storage_boundary"]["clean_schema"]
    }
    assert {"order_id", "user_id"} <= source_fields
    assert {"ip", "device_id"}.isdisjoint(source_fields)

    geo = _record(dimensions, "dimension_id", "geo")
    assert geo["source_fields"] == ["国家", "州/地区", "城市"]
    assert geo["data_contract_state"] == "contract_backed"
    assert geo["business_evidence_state"] == "quantifiable"
    assert geo["known_gaps"] == ["geo_device_environment_quality_policy"]

    device = _record(dimensions, "dimension_id", "device_environment")
    assert device["source_fields"] == ["设备品牌", "设备型号", "操作系统", "网络类型"]
    assert device["data_contract_state"] == "contract_backed"
    assert device["business_evidence_state"] == "quantifiable"
    assert device["known_gaps"] == ["geo_device_environment_quality_policy"]


def test_geo_device_factor_and_capability_are_available_with_quality_boundaries():
    factor_ledger = _load("contracts/ledger/factor-ledger.yaml")
    capability_support = _load("contracts/ledger/capability-support.yaml")
    backlog = _load("contracts/backlog/missing-contracts.yaml")

    factor = _record(
        factor_ledger["factor_groups"],
        "factor_group_id",
        "geo_device_environment",
    )
    assert factor["data_contract_state"] == "contract_backed"
    assert factor["default_business_evidence_state"] == "quantifiable"
    assert (
        "contracts/sources/paid-order-detail.source.yaml"
        in factor["source_refs"]["source_contracts"]
    )
    assert factor["known_gaps"] == [
        "geo_device_environment_quality_policy",
        "external_context_event_contracts",
    ]
    assert "limitation_refs" not in factor
    assert (
        "causal_attribution_without_mechanism_evidence" in factor["unsupported_grains"]
    )

    support = _record(
        capability_support["support_records"],
        "support_id",
        "joint_attribution_geo_device_contract_backed",
    )
    assert support["data_contract_state"] == "contract_backed"
    assert support["business_evidence_state"] == "quantifiable"
    assert support["evidence_type"] == "statistical_association"
    assert support["wording_limit"] == "candidate"
    assert "limitation_refs" not in support

    quality_policy = _record(
        backlog["backlog"],
        "backlog_id",
        "geo_device_environment_quality_policy",
    )
    assert quality_policy["data_contract_state"] == "contract_backed"
    assert quality_policy["source_refs"]["source_contract"] == (
        "contracts/sources/paid-order-detail.source.yaml"
    )
    assert "degrade only the affected output" in quality_policy["launch_impact"]


def test_raw_identifier_boundary_is_limited_to_present_user_and_order_ids():
    factor_ledger = _load("contracts/ledger/factor-ledger.yaml")
    capability_support = _load("contracts/ledger/capability-support.yaml")
    backlog = _load("contracts/backlog/missing-contracts.yaml")

    identifier_factor = _record(
        factor_ledger["factor_groups"],
        "factor_group_id",
        "sensitive_identity_and_dedup_fields",
    )
    assert identifier_factor["source_refs"]["source_template_fields"] == [
        "用户ID",
        "订单ID",
    ]
    assert identifier_factor["data_contract_state"] == "contract_backed"
    assert identifier_factor["default_business_evidence_state"] == "quantifiable"
    assert identifier_factor["limitation_refs"] == ["raw_identifier_output_limit"]

    limitation = _record(
        factor_ledger["review_limitations"],
        "limitation_id",
        "raw_identifier_output_limit",
    )
    assert limitation["data_contract_state"] == "unsupported_grain"
    assert "raw user and order identifiers are blocked" in limitation["description"]

    support = _record(
        capability_support["support_records"],
        "support_id",
        "dq_sensitive_identifier_output_boundary",
    )
    assert support["data_contract_state"] == "contract_backed"
    assert support["business_evidence_state"] == "contextual_evidence"
    assert support["limitation_refs"] == ["raw_identifier_output_limit"]

    policy = _record(
        backlog["backlog"],
        "backlog_id",
        "sensitive_identifier_output_policy",
    )
    assert policy["data_contract_state"] == "contract_backed"
    assert policy["affected_factor_groups"] == ["sensitive_identity_and_dedup_fields"]
    assert policy["source_refs"]["template_fields"] == ["用户ID", "订单ID"]
    assert (
        "safe aggregate geo, device, channel, and metric analysis remains independent"
        in policy["launch_impact"]
    )


def test_runtime_exposes_every_direct_geo_and_device_field_as_aggregate_only():
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    expected = {
        "country",
        "region",
        "city",
        "device_brand",
        "device_model",
        "os",
        "network_type",
    }

    assert expected <= set(registry.dimension_ids)
    for dimension_id in expected:
        dimension = registry.dimension(dimension_id)
        assert dimension["output_policy"] == "aggregate_only"
        if dimension_id == "country":
            assert dimension["decision_use"] == "scope_invariant"
            assert dimension["automatic_screening"] == "blocked"
        else:
            assert dimension["automatic_screening"] == "allowed"


def test_runtime_filter_contract_excludes_raw_identifiers_at_source_admission():
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    paid_order_filters = set(registry.customer_safe_filter_fields("paid_order_success"))
    assert {"channel", "region", "paid_amount_ngn"} <= paid_order_filters
    assert {"user_id", "order_id", "payment_started_ms"}.isdisjoint(paid_order_filters)
    assert set(registry.restricted_output_fields).isdisjoint(
        registry.all_customer_safe_filter_fields
    )
