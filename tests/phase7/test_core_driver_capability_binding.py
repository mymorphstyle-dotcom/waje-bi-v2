from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from bi_agent.capabilities.driver_decomposition import driver_decomposition
from bi_agent.runtime.analysis_contracts import (
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    MetricBinding,
    QueryContract,
    ResolvedWindow,
    ResultShape,
)
from bi_agent.runtime.authoritative_query_chain import (
    AuthoritativeQueryChainError,
    validate_capability_plan_semantics,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry


CORE_METRICS = (
    "paid_amount",
    "paid_users",
    "paid_orders",
    "first_paid_users",
    "paid_frequency",
    "avg_order_amount",
)


def _metric(metric_id: str) -> MetricBinding:
    return MetricBinding(
        metric_id=metric_id,
        contract_ref=f"metric:{metric_id}@1",
        dataset_id="paid_order_success",
        expression=metric_id,
        aggregation="sum",
        required_fields=(metric_id,),
        grain=("business_date",),
    )


def _component_query(metric_ids: tuple[str, ...]) -> QueryContract:
    required_fields = (
        "window_id",
        "window_role",
        "observation_key",
        *metric_ids,
    )
    return QueryContract(
        query_contract_id="query:component-driver-order",
        analysis_contract_ref="analysis:component-driver-order",
        query_intent="component_driver_scan",
        dataset_snapshot_refs=("snapshot:paid-order-success",),
        metric_bindings=tuple(_metric(metric_id) for metric_id in metric_ids),
        dimension_bindings=(),
        window_refs=("target_day", "previous_day"),
        resolved_windows=(
            ResolvedWindow(
                window_id="target_day",
                role="target",
                label="2026-06-01",
                start_inclusive="2026-06-01",
                end_exclusive="2026-06-02",
                timezone="Africa/Lagos",
                aggregation="daily",
                required_complete_days=1,
                source_watermark_requirement="2026-06-02",
            ),
            ResolvedWindow(
                window_id="previous_day",
                role="baseline",
                label="2026-05-31",
                start_inclusive="2026-05-31",
                end_exclusive="2026-06-01",
                timezone="Africa/Lagos",
                aggregation="daily",
                required_complete_days=1,
                source_watermark_requirement="2026-06-01",
            ),
        ),
        filters=(),
        result_shape=ResultShape(
            required_fields=required_fields,
            unique_key=("window_id", "observation_key"),
            grain=("window_id", "observation_key"),
            required_window_ids=("target_day", "previous_day"),
        ),
        completeness_assertions=("execution_succeeded",),
        permission_scope="analyst",
        workload_class="ordinary",
        contract_signature="query-signature",
    )


def _plan(
    capability_id: str,
    query: QueryContract,
    registry: RuntimeContractRegistry,
) -> CapabilityExecutionPlan:
    contract = registry.capability_inputs(capability_id)
    core_slot = CapabilityInputSlot(
        slot_id="component_driver_scan",
        query_contract_refs=(query.query_contract_id,),
        required=True,
        accepted_completeness=tuple(
            contract["minimum_readiness"]["accepted_completeness"]
        ),
        required_fields=query.result_shape.required_fields,
        required_window_ids=query.result_shape.required_window_ids,
    )
    optional_slots = ()
    if capability_id == "driver_decomposition":
        optional_slots = (
            CapabilityInputSlot(
                slot_id="payment_success_scan",
                query_contract_refs=(),
                required=False,
                accepted_completeness=tuple(
                    contract["minimum_readiness"]["accepted_completeness"]
                ),
                required_fields=(),
                required_window_ids=(),
            ),
        )
    maximum = str(contract["maximum_claim_strength"])
    return CapabilityExecutionPlan(
        capability_id=capability_id,
        capability_contract_ref=registry.capability_contract_ref(capability_id),
        required_input_slots=(core_slot,),
        optional_input_slots=optional_slots,
        merge_strategy=str(contract.get("merge_strategy") or "by_query_family"),
        minimum_readiness=dict(contract["minimum_readiness"]),
        degradation_policy=dict(contract["degradation_policy"]),
        supported_evidence_types=tuple(contract["supported_evidence_types"]),
        maximum_claim_strength=maximum,
        analysis_contract_ref="analysis:component-driver-order",
        supported_claim_types=tuple(contract["supported_claim_types"]),
        capability_contract_version=registry.contract_version,
        capability_contract_signature=registry.capability_contract_signature(
            capability_id
        ),
        claim_strength_taxonomy_version=registry.claim_strength_taxonomy_version,
        maximum_claim_strength_rank=registry.maximum_claim_strength_rank(maximum),
    )


@pytest.mark.parametrize(
    "capability_id",
    ("formula_decompose", "driver_decomposition"),
)
def test_component_metric_binding_accepts_contract_set_in_canonical_query_order(
    capability_id,
):
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    query = _component_query(tuple(sorted(CORE_METRICS)))

    validate_capability_plan_semantics(
        _plan(capability_id, query, registry),
        registry,
        {query.query_contract_id: query},
    )


@pytest.mark.parametrize(
    "metric_ids",
    (
        CORE_METRICS[:-1],
        (*CORE_METRICS[:-1], "paid_amount"),
    ),
)
def test_component_metric_binding_still_rejects_changed_or_duplicate_metric_set(
    metric_ids,
):
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    query = _component_query(metric_ids)

    with pytest.raises(
        AuthoritativeQueryChainError,
        match="capability_contract_slot_metrics_mismatch:component_driver_scan",
    ):
        validate_capability_plan_semantics(
            _plan("formula_decompose", query, registry),
            registry,
            {query.query_contract_id: query},
        )


def test_driver_decomposition_closes_three_core_factors_and_keeps_missing_success_neutral():
    evidence = driver_decomposition(
        (
            {
                "period": "comparison",
                "group": "baseline",
                "amount": 100.0,
                "paid_users": 10.0,
                "paid_frequency": 2.0,
                "avg_order_amount": 5.0,
                "first_paid_users": 3.0,
            },
            {
                "period": "comparison",
                "group": "target",
                "amount": 180.0,
                "paid_users": 12.0,
                "paid_frequency": 2.5,
                "avg_order_amount": 6.0,
                "first_paid_users": 4.0,
            },
        )
    )

    decomposition = evidence.typed_payload["decompositions"][0]
    contributions = decomposition["core_factor_contributions"]
    assert tuple(item["component_id"] for item in contributions) == (
        "paid_users",
        "paid_frequency",
        "avg_order_amount",
    )
    assert sum(item["contribution"] for item in contributions) == pytest.approx(
        decomposition["amount_delta"]
    )
    assert decomposition["core_reconciliation_residual"] == pytest.approx(0.0)
    assert decomposition["core_driver_ranking"][0] == "paid_frequency"
    assert decomposition["primary_core_driver"] == "paid_frequency"

    success = decomposition["payment_success_assumption"]
    assert success == {
        "component_id": "payment_success_rate",
        "status": "assumed_neutral",
        "baseline_value": 1.0,
        "target_value": 1.0,
        "contribution": 0.0,
        "observed": False,
    }
    payment_component = next(
        item
        for item in decomposition["component_changes"]
        if item["component_id"] == "payment_success_rate"
    )
    assert payment_component["status"] == "assumed_neutral"
    assert payment_component["observed"] is False


def test_driver_decomposition_exposes_only_authority_projectable_claim_numbers():
    evidence = driver_decomposition(
        (
            {
                "period": "comparison",
                "group": "baseline",
                "amount": 100.0,
                "paid_users": 10.0,
                "paid_frequency": 2.0,
                "avg_order_amount": 5.0,
            },
            {
                "period": "comparison",
                "group": "target",
                "amount": 180.0,
                "paid_users": 12.0,
                "paid_frequency": 2.5,
                "avg_order_amount": 6.0,
            },
        )
    )

    assert set(evidence.numeric_facts) == {
        "paid_users_contribution",
        "paid_users_contribution_share",
        "paid_frequency_contribution",
        "paid_frequency_contribution_share",
        "avg_order_amount_contribution",
        "avg_order_amount_contribution_share",
        "formula_contribution_total",
    }
    assert evidence.numeric_facts["formula_contribution_total"] == pytest.approx(80.0)
    assert evidence.numeric_facts["avg_order_amount_contribution_share"] == pytest.approx(
        0.3104166666666667
    )


def test_driver_contract_declares_units_for_every_publishable_claim_number():
    contract = yaml.safe_load(
        Path("contracts/capabilities/driver-decomposition.yaml").read_text()
    )
    fields = contract["evidence_outputs"]["claim_numeric_fields"]

    assert set(fields) == {
        "paid_users_contribution",
        "paid_users_contribution_share",
        "paid_frequency_contribution",
        "paid_frequency_contribution_share",
        "avg_order_amount_contribution",
        "avg_order_amount_contribution_share",
        "formula_contribution_total",
    }
    for field, spec in fields.items():
        if field.endswith("_share"):
            assert spec["value_semantics"] == "signed_unbounded_ratio"
            assert spec["display_format"] == "percent"
        else:
            assert spec["value_semantics"] == "target_metric_scalar"
            assert spec["display_format"] == "number"
        assert spec["derivation"] == "multiplicative_shapley_v1"


def test_driver_decomposition_consumes_canonical_authoritative_component_rows():
    evidence = driver_decomposition(
        (
            {
                "window_id": "previous_day",
                "window_role": "baseline",
                "observation_key": "2026-05-31",
                "paid_amount": 100.0,
                "paid_users": 10.0,
                "paid_orders": 20.0,
                "paid_frequency": 2.0,
                "avg_order_amount": 5.0,
                "first_paid_users": 3.0,
            },
            {
                "window_id": "target_day",
                "window_role": "target",
                "observation_key": "2026-06-01",
                "paid_amount": 180.0,
                "paid_users": 12.0,
                "paid_orders": 30.0,
                "paid_frequency": 2.5,
                "avg_order_amount": 6.0,
                "first_paid_users": 4.0,
            },
        )
    )

    assert evidence.limitations == ()
    decomposition = evidence.typed_payload["decompositions"][0]
    assert decomposition["amount_delta"] == pytest.approx(80.0)
    assert decomposition["primary_core_driver"] == "paid_frequency"
    assert decomposition["core_reconciliation_status"] == "reconciled"
