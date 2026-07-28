from __future__ import annotations

from datetime import date, datetime, timedelta

from bi_agent.capabilities.event_evidence import event_evidence
from bi_agent.runtime.analysis_contracts import (
    MetricBinding,
    QueryContract,
    ResolvedWindow,
    ResultShape,
    query_contract_signature,
)
from bi_agent.runtime.event_window_derivation import (
    derive_event_window_set,
    validate_event_window_derivation_policy,
    validate_event_window_set,
)
from bi_agent.runtime.query_ir import compile_capability_query_route
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.runtime.window_metric_evidence import (
    aggregate_derived_event_window_set,
)
from tests.support.temporal_authority import resolved_test_temporal_authority


def _authority():
    return resolved_test_temporal_authority(
        time_spec={
            "kind": "date_range",
            "start": "2024-01-01",
            "end": "2024-03-31",
        },
        comparison_spec={
            "kind": "calendar_partition",
            "baseline_class": "same_month_phase",
            "period_grain": "month",
            "partition_field": "month_phase",
            "target_members": ["start"],
            "baseline_members": ["mid", "end"],
            "aggregation": "mean_of_complete_days",
            "member_definitions": [
                {"member": "start", "day_start": 1, "day_end": 10},
                {"member": "mid", "day_start": 11, "day_end": 20},
                {"member": "end", "day_start": 21, "day_end": 31},
            ],
        },
        require_physical_baseline=False,
    )


def _event() -> dict[str, object]:
    return {
        "window_id": "target_day",
        "window_role": "target",
        "observation_key": "event:monthly-campaign",
        "event_count": 1,
        "source_family": "external_event",
        "event_id": "event:monthly-campaign",
        "event_type": "campaign",
        "event_start_date": "2024-01-01",
        "event_end_date": "2024-03-31",
        "affected_scope": "Nigeria",
        "authority": "reviewed_workbook",
        "evidence_level": "context",
        "wording_limit": "context",
        "recurrence_kind": "monthly_day_range",
        "recurrence_month_start": 0,
        "recurrence_day_start": 10,
        "recurrence_month_end": 0,
        "recurrence_day_end": 12,
        "payload": "{}",
    }


def _contract(authority) -> QueryContract:
    start = date.fromisoformat(authority.target_window.start)
    end = date.fromisoformat(authority.target_window.end) + timedelta(days=1)
    window = ResolvedWindow(
        window_id="target_day",
        role="target",
        label="evaluation range",
        start_inclusive=start.isoformat(),
        end_exclusive=end.isoformat(),
        timezone="Africa/Lagos",
        aggregation="mean_of_complete_days",
        required_complete_days=(end - start).days,
        source_watermark_requirement=(end - timedelta(days=1)).isoformat(),
        capability_refs=("event_window_compare",),
    )
    metric = MetricBinding(
        metric_id="paid_amount",
        contract_ref="contract:metric:paid_amount",
        dataset_id="paid_order_success",
        expression="sum(paid_amount)",
        aggregation="sum",
        required_fields=("paid_amount",),
        grain=("window_id", "observation_key"),
    )
    logical = {
        "analysis_contract_ref": "analysis:dynamic-event-window",
        "query_intent": "daily_metric_baselines",
        "dataset_snapshot_refs": ("snapshot:paid:1",),
        "metric_bindings": (metric,),
        "dimension_bindings": (),
        "window_refs": ("target_day",),
        "resolved_windows": (window,),
        "filters": (),
        "result_shape": ResultShape(
            required_fields=(
                "window_id",
                "window_role",
                "observation_key",
                "paid_amount",
            ),
            unique_key=("window_id", "observation_key"),
            grain=("window_id", "observation_key"),
            required_window_ids=("target_day",),
        ),
        "completeness_assertions": (
            "required_fields_present",
            "required_windows_complete",
        ),
        "workload_class": "interactive_aggregate",
    }
    return QueryContract(
        query_contract_id="query:dynamic-event-window",
        contract_signature=query_contract_signature(logical),
        **logical,
    )


def test_calendar_partition_routes_event_impact_through_discovery_join() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    route = compile_capability_query_route(
        capability_id="event_window_compare",
        capability_contract=registry.capability_inputs("event_window_compare"),
        temporal_authority=_authority(),
    )

    assert route["status"] == "derived_observation_frame"
    assert route["adapter_kind"] == "event_evidence_join_frame"
    assert route["window_roles"] == ["target"]
    assert route["boundary_code"] is None


def test_event_window_policy_accepts_immutable_sequence_round_trip() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    policy = registry.capability_inputs("event_window_compare")[
        "dynamic_event_window_policy"
    ]
    frozen_policy = {
        **policy,
        "eligible_parent_modes": tuple(policy["eligible_parent_modes"]),
    }

    assert validate_event_window_derivation_policy(frozen_policy) == policy


def test_discovered_recurring_event_derives_complete_pre_and_post_windows() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    authority = _authority()
    policy = registry.capability_inputs("event_window_compare")[
        "dynamic_event_window_policy"
    ]

    derived = derive_event_window_set(
        (_event(),),
        temporal_authority=authority,
        policy=policy,
    )
    validated = validate_event_window_set(
        derived,
        temporal_authority=authority,
        policy=policy,
    )

    assert validated["source_event_count"] == 1
    assert validated["derived_occurrence_count"] == 3
    assert tuple(
        (
            item["event_start_date"],
            item["event_end_date"],
            item["baseline_start_date"],
            item["baseline_end_date"],
            item["target_start_date"],
            item["target_end_date"],
        )
        for item in validated["occurrences"]
    ) == (
        (
            "2024-01-10",
            "2024-01-12",
            "2024-01-07",
            "2024-01-09",
            "2024-01-13",
            "2024-01-15",
        ),
        (
            "2024-02-10",
            "2024-02-12",
            "2024-02-07",
            "2024-02-09",
            "2024-02-13",
            "2024-02-15",
        ),
        (
            "2024-03-10",
            "2024-03-12",
            "2024-03-07",
            "2024-03-09",
            "2024-03-13",
            "2024-03-15",
        ),
    )


def test_event_dates_from_query_drivers_are_normalized_to_business_dates() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    authority = _authority()
    policy = registry.capability_inputs("event_window_compare")[
        "dynamic_event_window_policy"
    ]
    event = {
        **_event(),
        "event_start_date": datetime(2024, 1, 1, 8, 0),
        "event_end_date": datetime(2024, 3, 31, 23, 0),
    }

    derived = derive_event_window_set(
        (event,),
        temporal_authority=authority,
        policy=policy,
    )

    assert derived["derived_occurrence_count"] == 3
    assert derived["occurrences"][0]["event_start_date"] == "2024-01-10"


def test_dynamic_comparison_consumes_daily_metric_frame_for_every_occurrence() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    authority = _authority()
    policy = registry.capability_inputs("event_window_compare")[
        "dynamic_event_window_policy"
    ]
    derived = derive_event_window_set(
        (_event(),),
        temporal_authority=authority,
        policy=policy,
    )
    start = date(2024, 1, 1)
    end = date(2024, 4, 1)
    post_days = {
        date.fromisoformat(item["target_start_date"]) + timedelta(days=offset)
        for item in derived["occurrences"]
        for offset in range(item["required_complete_days"])
    }
    rows = tuple(
        {
            "window_id": "target_day",
            "window_role": "target",
            "observation_key": (start + timedelta(days=offset)).isoformat(),
            "paid_amount": (
                20 if start + timedelta(days=offset) in post_days else 10
            ),
        }
        for offset in range((end - start).days)
    )

    result = aggregate_derived_event_window_set(
        _contract(authority),
        rows,
        metric_id="paid_amount",
        event_window_set=derived,
        temporal_authority=authority,
        derivation_policy=policy,
    )

    assert result["event_occurrence_count"] == 3
    assert {item["direction"] for item in result["comparisons"]} == {"higher"}
    assert {item["baseline_value"] for item in result["comparisons"]} == {10}
    assert {item["target_value"] for item in result["comparisons"]} == {20}
    assert {item["relative_change"] for item in result["comparisons"]} == {1}


def test_event_presence_and_comparison_share_dynamic_identity() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    authority = _authority()
    policy = registry.capability_inputs("event_window_compare")[
        "dynamic_event_window_policy"
    ]
    derived = derive_event_window_set(
        (_event(),),
        temporal_authority=authority,
        policy=policy,
    )

    evidence = event_evidence(
        (_event(),),
        event_window_set=derived,
    )

    assert evidence.typed_payload["event_ref"] == derived["event_ref"]
    assert evidence.typed_payload["temporal_authority_ref"] == (
        derived["temporal_authority_ref"]
    )
    assert evidence.typed_payload["source_temporal_authority_ref"] == (
        authority.authority_ref
    )
    assert len(evidence.typed_payload["event_occurrence_summary"]) == 3
