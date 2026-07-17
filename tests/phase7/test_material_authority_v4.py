from bi_agent.conversation.clarification_authority import (
    _compiled_goal_material_projection,
    build_material_authority,
    validate_material_authority,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


_SOURCE_RUN_ID = "run-material-authority-v4"
_THREAD_ID = "thread-material-authority-v4"
_TOPIC_ID = "topic-material-authority-v4"
_GOAL_BINDINGS = [{"goal_id": "explain_change", "role": "primary"}]
_EXPLICIT_FOCUS = {
    "component_ids": [],
    "dimension_ids": [],
    "context_source_ids": [],
}
_ROUTE_FIELDS = {
    "target_metrics",
    "component_ids",
    "association_metric_ids",
    "dimension_ids",
    "baselines",
    "context_sources",
    "claim_types",
    "required_outcomes",
    "analysis_axis_ids",
    "diagnostic_tags",
    "scope",
}
def _compiled_explain_change_material():
    registry = RuntimeContractRegistry.from_path(
        CANONICAL_RUNTIME_BINDINGS_PATH
    )
    plan = registry.compile_goal_analysis_plan(
        goal_bindings=_GOAL_BINDINGS,
        target_metric="paid_amount",
        explicit_focus=_EXPLICIT_FOCUS,
    )
    projection = _compiled_goal_material_projection(
        goal_bindings=_GOAL_BINDINGS,
        target_metric="paid_amount",
        explicit_focus=_EXPLICIT_FOCUS,
        runtime_registry=registry,
    )
    assert projection["analysis_axis_ids"] == [
        axis["axis_id"] for axis in plan["analysis_axes"]
    ]
    return registry, projection


def _runtime_material(registry):
    return {
        "schema_version": "3",
        "target_semantic": "2026-06-01",
        "as_of": "2026-07-16T12:00:00+08:00",
        "business_timezone": "Asia/Shanghai",
        "context_window_specs": [],
        "fixed_window_bounds": {
            "target_day": ["2026-06-01", "2026-06-01"],
            "previous_day": ["2026-05-31", "2026-05-31"],
            "rolling_7_day_baseline": ["2026-05-25", "2026-05-31"],
            "same_weekday_last_week": ["2026-05-25", "2026-05-25"],
            "pattern_history": ["2026-01-01", "2026-06-01"],
            "anomaly_history": ["2026-05-02", "2026-05-31"],
        },
        "filters": [],
        "grain": "window_id",
        "dataset_requirements": [],
        "metric_dataset_overrides": {},
        "dimension_dataset_overrides": {},
        "requested_context_sources": [],
        "accepted_graph": ["compare_periods"],
        "runtime_contract_version": registry.contract_version,
        "runtime_registry_digest": registry.source_payload_digest,
        "run_mode_class": "authoritative",
        "source_query_contracts": [],
    }


def _material_authority():
    registry, projection = _compiled_explain_change_material()
    authority = build_material_authority(
        source_run_id=_SOURCE_RUN_ID,
        thread_id=_THREAD_ID,
        topic_id=_TOPIC_ID,
        original_intent={
            "question_family": "paid_amount_change_explanation",
            "question_families": ["paid_amount_change_explanation"],
            "primary_question_family": "paid_amount_change_explanation",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
            "goal_bindings": _GOAL_BINDINGS,
            "explicit_focus": _EXPLICIT_FOCUS,
            "baseline_candidates": ["previous_day"],
            "scope": "full_sample",
            "time_window": {
                "target": "2026-06-01",
                "baseline": "previous_day",
            },
        },
        material_slots={
            "target_metrics": ["paid_amount"],
            "component_ids": projection["component_ids"],
            "association_metric_ids": projection["association_metric_ids"],
            "dimension_ids": projection["dimension_ids"],
            "baselines": ["previous_day"],
            "context_sources": projection["context_sources"],
            "claim_types": projection["claim_types"],
            "required_outcomes": projection["required_outcomes"],
            "analysis_axis_ids": projection["analysis_axis_ids"],
            "diagnostic_tags": [],
            "scope": "full_sample",
        },
        runtime_material=_runtime_material(registry),
    )
    return authority, projection


def test_material_authority_v4_builds_and_validates_from_current_goal_registry():
    authority, projection = _material_authority()

    validated = validate_material_authority(
        authority,
        source_run_id=_SOURCE_RUN_ID,
        thread_id=_THREAD_ID,
        topic_id=_TOPIC_ID,
        require_execution_material=True,
    )

    assert validated == authority
    assert validated["schema_version"] == "4"
    assert set(validated["route_material_slots"]) == _ROUTE_FIELDS
    assert validated["intent_material"]["goal_bindings"] == _GOAL_BINDINGS
    assert validated["intent_material"]["explicit_focus"] == {
        "component_ids": [],
        "context_source_ids": [],
        "dimension_ids": [],
    }
    for axis in (
        "component_ids",
        "association_metric_ids",
        "dimension_ids",
        "context_sources",
        "claim_types",
        "required_outcomes",
        "analysis_axis_ids",
    ):
        assert validated["intent_material"][axis] == projection[axis]


def test_execution_material_signs_parameterized_context_window():
    registry, projection = _compiled_explain_change_material()
    context_spec = {
        "capability_id": "rolling_window_compare",
        "relation": "trailing_complete_periods",
        "unit": "day",
        "count": 7,
    }
    runtime_material = _runtime_material(registry)
    runtime_material["context_window_specs"] = [context_spec]
    runtime_material["accepted_graph"] = [
        "compare_periods",
        "rolling_window_compare",
    ]
    runtime_material["fixed_window_bounds"][
        "context__rolling_window_compare__trailing_complete_periods__7_day"
    ] = ["2026-05-25", "2026-05-31"]
    authority = build_material_authority(
        source_run_id=_SOURCE_RUN_ID,
        thread_id=_THREAD_ID,
        topic_id=_TOPIC_ID,
        original_intent={
            "question_family": "paid_amount_change_explanation",
            "question_families": ["paid_amount_change_explanation"],
            "primary_question_family": "paid_amount_change_explanation",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
            "goal_bindings": _GOAL_BINDINGS,
            "explicit_focus": _EXPLICIT_FOCUS,
            "baseline_candidates": ["previous_day"],
            "scope": "full_sample",
            "time_window": "2026-06-01",
        },
        material_slots={
            "target_metrics": ["paid_amount"],
            "component_ids": projection["component_ids"],
            "association_metric_ids": projection["association_metric_ids"],
            "dimension_ids": projection["dimension_ids"],
            "baselines": ["previous_day"],
            "context_sources": projection["context_sources"],
            "claim_types": projection["claim_types"],
            "required_outcomes": projection["required_outcomes"],
            "analysis_axis_ids": projection["analysis_axis_ids"],
            "diagnostic_tags": [],
            "scope": "full_sample",
        },
        runtime_material=runtime_material,
    )

    validated = validate_material_authority(
        authority,
        source_run_id=_SOURCE_RUN_ID,
        thread_id=_THREAD_ID,
        topic_id=_TOPIC_ID,
        require_execution_material=True,
    )
    execution_material = validated["execution_material"]
    assert execution_material["context_window_specs"] == [context_spec]
    assert (
        "context__rolling_window_compare__trailing_complete_periods__7_day"
        in execution_material["fixed_window_bounds"]
    )
