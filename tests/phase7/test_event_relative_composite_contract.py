from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bi_agent.runtime.analysis_contracts import (
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    MetricBinding,
    QueryContract,
    ResolvedWindow,
    ResultShape,
    query_contract_signature,
)
from bi_agent.runtime.authoritative_task_inputs import (
    AuthoritativeTaskInputContractError,
    _event_window_metric_comparison_payload,
)
from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.runtime.window_metric_evidence import (
    WindowMetricEvidenceError,
    validate_event_window_metric_authority,
)
from tests.support.temporal_authority import resolved_test_temporal_authority


def _authority():
    return resolved_test_temporal_authority(
        time_spec={"kind": "date", "target": "2026-06-19"},
        comparison_spec={
            "kind": "event_relative_window",
            "event_ref": "business-event:campaign-june-2026",
            "target_start": "2026-06-19",
            "target_end": "2026-06-19",
            "baseline_start": "2026-06-18",
            "baseline_end": "2026-06-18",
            "aggregation": "sum_of_complete_days",
        },
        require_physical_baseline=True,
    )


def _query(
    *,
    metric_id: str = "paid_amount",
    target_window_id: str = "target_day",
    baseline_window_id: str = "baseline_window",
) -> QueryContract:
    windows = (
        ResolvedWindow(
            window_id=target_window_id,
            role="target",
            label="2026-06-19",
            start_inclusive="2026-06-19",
            end_exclusive="2026-06-20",
            timezone="Africa/Lagos",
            aggregation="sum_of_complete_days",
            required_complete_days=1,
            source_watermark_requirement="2026-06-19",
            capability_refs=("event_window_compare",),
        ),
        ResolvedWindow(
            window_id=baseline_window_id,
            role="baseline",
            label="2026-06-18",
            start_inclusive="2026-06-18",
            end_exclusive="2026-06-19",
            timezone="Africa/Lagos",
            aggregation="sum_of_complete_days",
            required_complete_days=1,
            source_watermark_requirement="2026-06-18",
            capability_refs=("event_window_compare",),
        ),
    )
    metric = MetricBinding(
        metric_id=metric_id,
        contract_ref=f"contract:metric:{metric_id}",
        dataset_id="paid_order_success",
        expression=f"sum({metric_id})",
        aggregation="sum",
        required_fields=(metric_id,),
        grain=("window_id", "observation_key"),
    )
    logical = {
        "analysis_contract_ref": "analysis:event-window",
        "query_intent": "daily_metric_baselines",
        "dataset_snapshot_refs": ("snapshot:paid:r1",),
        "metric_bindings": (metric,),
        "dimension_bindings": (),
        "window_refs": tuple(item.window_id for item in windows),
        "resolved_windows": windows,
        "filters": (),
        "result_shape": ResultShape(
            required_fields=(
                "window_id",
                "window_role",
                "observation_key",
                metric_id,
            ),
            unique_key=("window_id", "observation_key"),
            grain=("window_id", "observation_key"),
            required_window_ids=tuple(item.window_id for item in windows),
        ),
        "completeness_assertions": (
            "required_fields_present",
            "required_windows_complete",
        ),
        "workload_class": "interactive_aggregate",
    }
    return QueryContract(
        query_contract_id="query:event-window",
        contract_signature=query_contract_signature(logical),
        **logical,
    )


def _execution_plan(contract: QueryContract) -> CapabilityExecutionPlan:
    slot = CapabilityInputSlot(
        slot_id="daily_metric_baselines",
        query_contract_refs=(contract.query_contract_id,),
        required=True,
        accepted_completeness=("complete",),
        required_fields=contract.result_shape.required_fields,
        required_window_ids=contract.result_shape.required_window_ids,
    )
    return CapabilityExecutionPlan(
        capability_id="event_window_compare",
        capability_contract_ref="contract:capability:event-window-compare",
        required_input_slots=(slot,),
        optional_input_slots=(),
        merge_strategy="by_query_family",
        minimum_readiness={
            "required_slots": "all",
            "accepted_completeness": ("complete",),
        },
        degradation_policy={"missing_required_input": "block_candidate_impact"},
        supported_evidence_types=("observed_comparison",),
        maximum_claim_strength="directional",
        analysis_contract_ref=contract.analysis_contract_ref,
        supported_claim_types=("business_object_candidate_impact",),
    )


def _rows(metric_id: str = "paid_amount") -> tuple[dict[str, object], ...]:
    return (
        {
            "window_id": "target_day",
            "window_role": "target",
            "observation_key": "2026-06-19",
            metric_id: 120,
        },
        {
            "window_id": "baseline_window",
            "window_role": "baseline",
            "observation_key": "2026-06-18",
            metric_id: 100,
        },
    )


def test_registry_declares_the_event_composite_and_task_dependency() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    event_window = registry.capability_inputs("event_window_compare")
    event_presence = registry.capability_inputs("event_evidence")
    assert event_window["task_dependencies"] == ["event_evidence"]
    assert event_window["task_input_binding"] == {
        "payload_kind": "event_window_metric_comparison",
        "query_families": {"primary": "daily_metric_baselines"},
    }
    assert event_window["required_metrics"] == ["paid_amount"]
    assert event_window["evidence_contract"] == ("event-window-metric-comparison.v1")
    assert event_presence["task_input_binding"] == {
        "payload_kind": "event_evidence",
        "query_families": {"primary": "event_context_probe"},
    }
    assert event_presence["evidence_contract"] == "event-presence.v1"
    assert registry.claim_composite_support_policy(
        "business_object_candidate_impact"
    ) == {
        "policy": "all_required_supports_same_authority",
        "claim_class": "candidate_impact",
        "publication_strength": "candidate_driver",
        "causal_interpretation_allowed": False,
        "identity_fields": ["event_ref", "temporal_authority_ref"],
        "required_supports": [
            {
                "source_claim_kind": "business_object_candidate_impact",
                "evidence_kind": "observed",
                "maximum_claim_strength": "directional",
                "evidence_contract": "event-window-metric-comparison.v1",
            },
            {
                "source_claim_kind": "candidate_mechanism",
                "evidence_kind": "observed",
                "maximum_claim_strength": "candidate_mechanism",
                "evidence_contract": "event-presence.v1",
            },
        ],
    }


@pytest.mark.parametrize(
    ("mutate", "error"),
    (
        (
            lambda payload: payload["capability_inputs"]["event_window_compare"].update(
                task_dependencies=["invented_capability"]
            ),
            "runtime_capability_task_dependencies_invalid:event_window_compare",
        ),
        (
            lambda payload: payload["capability_inputs"]["event_evidence"].update(
                task_dependencies=["event_window_compare"]
            ),
            "runtime_capability_task_dependency_cycle",
        ),
        (
            lambda payload: payload["claim_publication_policy"][
                "composite_support_by_claim_kind"
            ]["business_object_candidate_impact"].update(
                causal_interpretation_allowed=True
            ),
            "runtime_claim_composite_support_policy_invalid",
        ),
        (
            lambda payload: payload["claim_publication_policy"][
                "composite_support_by_claim_kind"
            ]["business_object_candidate_impact"].update(required_supports=[]),
            "runtime_claim_composite_support_requirements_invalid",
        ),
    ),
)
def test_registry_rejects_event_composite_contract_drift(mutate, error: str) -> None:
    payload = deepcopy(load_contract(CANONICAL_RUNTIME_BINDINGS_PATH))
    mutate(payload)

    with pytest.raises(ValueError, match=error):
        RuntimeContractRegistry(payload)


def test_event_window_materializer_uses_only_the_business_metric_contract() -> None:
    authority = _authority()
    contract = _query()
    rows = _rows()
    execution_plan = _execution_plan(contract)
    bound = SimpleNamespace(
        query_contract_refs=(contract.query_contract_id,),
        rows_by_slot={"daily_metric_baselines": rows},
    )
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    payload = _event_window_metric_comparison_payload(
        bound=bound,
        execution_plan=execution_plan,
        contracts=(contract,),
        metric_id="paid_amount",
        binding=registry.capability_inputs("event_window_compare")[
            "task_input_binding"
        ],
        capability_id="event_window_compare",
        temporal_authority=authority,
    )

    assert payload == {
        "contract": contract,
        "rows": rows,
        "metric_id": "paid_amount",
        "primary_baseline_window_id": "baseline_window",
        "event_ref": authority.event_ref,
        "temporal_authority_ref": authority.authority_ref,
    }


def test_event_window_materializer_rejects_event_count_as_the_impact_metric() -> None:
    authority = _authority()
    contract = _query(metric_id="event_count")
    execution_plan = _execution_plan(contract)
    bound = SimpleNamespace(
        query_contract_refs=(contract.query_contract_id,),
        rows_by_slot={"daily_metric_baselines": _rows("event_count")},
    )
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    with pytest.raises(
        AuthoritativeTaskInputContractError,
        match="authoritative_event_window_metric_forbidden",
    ):
        _event_window_metric_comparison_payload(
            bound=bound,
            execution_plan=execution_plan,
            contracts=(contract,),
            metric_id="event_count",
            binding=registry.capability_inputs("event_window_compare")[
                "task_input_binding"
            ],
            capability_id="event_window_compare",
            temporal_authority=authority,
        )


def test_event_window_physical_contract_must_match_frozen_temporal_material() -> None:
    authority = _authority()
    contract = _query()
    validate_event_window_metric_authority(
        contract,
        authority,
        primary_baseline_window_id="baseline_window",
    )

    drifted_window = replace(
        contract.resolved_windows[0],
        aggregation="mean_of_complete_days",
    )
    drifted = replace(
        contract,
        resolved_windows=(drifted_window, contract.resolved_windows[1]),
    )
    drifted = replace(
        drifted,
        contract_signature=query_contract_signature(drifted),
    )

    with pytest.raises(
        WindowMetricEvidenceError,
        match="event_window_temporal_material_drift",
    ):
        validate_event_window_metric_authority(
            drifted,
            authority,
            primary_baseline_window_id="baseline_window",
        )


def test_event_window_physical_ids_cannot_replace_frozen_authority_refs() -> None:
    authority = _authority()
    contract = _query(
        target_window_id=authority.target_window.window_ref,
        baseline_window_id=authority.baseline_window.window_ref,
    )

    with pytest.raises(
        WindowMetricEvidenceError,
        match="event_window_physical_windows_invalid",
    ):
        validate_event_window_metric_authority(
            contract,
            authority,
            primary_baseline_window_id=authority.baseline_window.window_ref,
        )
