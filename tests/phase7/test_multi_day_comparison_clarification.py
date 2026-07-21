from __future__ import annotations

from copy import deepcopy

import pytest

from bi_agent.conversation.agent_core import (
    _validated_single_authority_decision_binding,
)
from bi_agent.runtime import langgraph_workflow
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.llm_client import LLMOutputError
from bi_agent.runtime.llm_prompts import (
    SINGLE_AUTHORITY_PROMPT_VERSION,
    build_prompt,
)
from bi_agent.runtime.single_authority import (
    DecisionLedger,
    DecisionRecord,
    IntentRevision,
    SingleAuthorityContractError,
)
from bi_agent.runtime.temporal_comparison import (
    COMPARISON_WINDOW_VALUE_REFS,
    EffectiveTemporalComparison,
    TemporalComparisonContractError,
    resolve_effective_comparison,
    temporal_decision_option_id,
    validate_comparison_spec,
)


TARGET_RANGE = {
    "kind": "date_range",
    "start": "2026-04-01",
    "end": "2026-06-30",
}


def _comparison_window_slot() -> dict[str, object]:
    return next(
        dict(slot)
        for slot in langgraph_workflow._single_authority_ambiguity_slot_catalog()
        if slot["slot_id"] == "comparison_window"
    )


def test_provider_adapter_removes_only_matching_redundant_fixed_window_target() -> None:
    binding = {
        "time_spec": {"kind": "date", "target": "2026-05-20"},
        "comparison_spec": {
            "kind": "fixed_window",
            "target_start": "2026-05-20",
            "target_end": "2026-05-20",
            "baseline_class": "prior_period",
            "baseline_start": "2026-05-19",
            "baseline_end": "2026-05-19",
            "aggregation": "sum_of_complete_days",
        },
    }

    normalized = langgraph_workflow._normalize_provider_intent_binding(binding)

    assert normalized["comparison_spec"] == {
        "kind": "fixed_window",
        "baseline_class": "prior_period",
        "baseline_start": "2026-05-19",
        "baseline_end": "2026-05-19",
        "aggregation": "sum_of_complete_days",
    }
    assert "target_start" in binding["comparison_spec"]


def test_provider_adapter_preserves_conflicting_fixed_window_target_for_rejection() -> (
    None
):
    binding = {
        "time_spec": {"kind": "date", "target": "2026-05-20"},
        "comparison_spec": {
            "kind": "fixed_window",
            "target_start": "2026-05-21",
            "target_end": "2026-05-21",
            "baseline_class": "prior_period",
            "baseline_start": "2026-05-19",
            "baseline_end": "2026-05-19",
            "aggregation": "sum_of_complete_days",
        },
    }

    normalized = langgraph_workflow._normalize_provider_intent_binding(binding)

    with pytest.raises(
        TemporalComparisonContractError,
        match="temporal_comparison_spec_invalid",
    ):
        validate_comparison_spec(
            normalized["comparison_spec"],
            time_spec=normalized["time_spec"],
        )


def _intent(
    *,
    time_spec: dict[str, object],
    slot_id: str,
    extra_slots: tuple[dict[str, object], ...] = (),
) -> IntentRevision:
    catalog = {
        str(slot["slot_id"]): slot
        for slot in langgraph_workflow._single_authority_ambiguity_slot_catalog()
    }
    selected = catalog[slot_id]
    ambiguity_slots = (
        {
            "slot_id": slot_id,
            "slot_kind": selected["slot_kind"],
            "materiality": selected["materiality"],
            "status": "unresolved",
            "question": "请选择本次比较窗口。",
            "allowed_value_refs": selected["allowed_value_refs"],
        },
        *extra_slots,
    )
    original = "分析指定时间范围内的业务指标变化。"
    return IntentRevision.create(
        run_attempt_id="run-comparison-window",
        original_user_text=original,
        goal_bindings=({"goal_id": "explain_change", "role": "primary"},),
        target_metric_refs=("paid_amount",),
        scope={"scope_type": "full_sample", "filters": []},
        time_spec=time_spec,
        comparison_spec={"kind": "decision_slot", "slot_id": slot_id},
        direction_premise="unknown",
        requested_analysis_axes=(),
        desired_decisions=(),
        ambiguity_slots=ambiguity_slots,
        source_spans=(
            {
                "field": "original_user_text",
                "start": 0,
                "end": len(original),
                "text": original,
            },
        ),
        schema_version="intent-revision.v1",
        prompt_version=SINGLE_AUTHORITY_PROMPT_VERSION,
        model_version="provider-test",
        known_ambiguity_value_refs={
            "previous_day",
            "rolling_7_day_baseline",
            "same_weekday_last_week",
            *COMPARISON_WINDOW_VALUE_REFS,
        },
        known_ambiguity_slots=catalog,
    )


@pytest.mark.parametrize(
    "typed_value",
    (
        {
            "kind": "fixed_window",
            "baseline_class": "prior_period",
            "baseline_start": "2026-01-01",
            "baseline_end": "2026-03-31",
            "aggregation": "sum_of_complete_days",
        },
        {
            "kind": "fixed_window",
            "baseline_class": "same_period_last_year",
            "baseline_start": "2025-04-01",
            "baseline_end": "2025-06-30",
            "aggregation": "sum_of_complete_days",
        },
        {
            "kind": "fixed_window",
            "baseline_class": "custom_control_window",
            "baseline_start": "2024-10-01",
            "baseline_end": "2024-12-31",
            "aggregation": "mean_of_complete_days",
        },
    ),
)
def test_date_range_decision_resolves_complete_comparison_window(
    typed_value: dict[str, object],
) -> None:
    intent = _intent(time_spec=TARGET_RANGE, slot_id="comparison_window")
    option_id = temporal_decision_option_id(
        slot_id="comparison_window",
        value=typed_value,
        time_spec=intent.time_spec,
    )
    decision = DecisionRecord.create(
        intent_revision_id=intent.intent_revision_id,
        slot_id="comparison_window",
        value=typed_value,
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=("baseline_refs", "resolved_window_refs"),
        option_id=option_id,
    )
    ledger = DecisionLedger().append(decision).append(decision)

    authority = resolve_effective_comparison(
        time_spec=intent.time_spec,
        comparison_spec=intent.comparison_spec,
        decision_ledger=ledger,
        require_physical_baseline=True,
    )

    assert len(ledger.records) == 1
    assert authority.mode == "window_pair"
    assert authority.source == "decision"
    assert authority.effective_comparison_spec == typed_value
    assert authority.baseline_window is not None
    assert authority.baseline_window.start == typed_value["baseline_start"]
    assert authority.baseline_window.end == typed_value["baseline_end"]


def test_comparison_window_provider_options_carry_complete_typed_specs() -> None:
    slot = _comparison_window_slot()
    output = {
        "question": "这段时间应采用哪个业务比较窗口？",
        "options": [
            {
                "value_ref": "prior_period",
                "typed_value": {
                    "kind": "fixed_window",
                    "baseline_class": "prior_period",
                    "baseline_start": "2026-01-01",
                    "baseline_end": "2026-03-31",
                    "aggregation": "sum_of_complete_days",
                },
                "label": "与上一完整周期比较（推荐）",
                "description": "观察相邻完整周期的整体变化。",
                "recommended": True,
            },
            {
                "value_ref": "same_period_last_year",
                "typed_value": {
                    "kind": "fixed_window",
                    "baseline_class": "same_period_last_year",
                    "baseline_start": "2025-04-01",
                    "baseline_end": "2025-06-30",
                    "aggregation": "sum_of_complete_days",
                },
                "label": "与去年同期比较",
                "description": "观察跨年度同期变化。",
                "recommended": False,
            },
            {
                "value_ref": "custom_control_window",
                "typed_value": {
                    "kind": "fixed_window",
                    "baseline_class": "custom_control_window",
                    "baseline_start": "2024-10-01",
                    "baseline_end": "2024-12-31",
                    "aggregation": "mean_of_complete_days",
                },
                "label": "与已明确的控制窗口比较",
                "description": "按业务指定控制期观察差异。",
                "recommended": False,
            },
        ],
        "recommendation_reason": "相邻完整周期最便于解释当前变化。",
        "status_message": "等待确认比较窗口。",
    }

    normalized = langgraph_workflow._validate_single_authority_clarification_output(
        output,
        slot=slot,
        time_spec=TARGET_RANGE,
        required_recommended_value_ref="",
        required_recommended_label="",
    )
    records = [
        langgraph_workflow._single_authority_decision_option_record(
            slot=slot,
            time_spec=TARGET_RANGE,
            option=option,
        )
        for option in normalized
    ]

    assert [record["typed_value"] for record in records] == [
        option["typed_value"] for option in normalized
    ]
    assert all(
        record["option_id"].startswith(f"comparison_window.{option['value_ref']}.")
        for record, option in zip(records, normalized, strict=True)
    )

    changed_wording = deepcopy(normalized[0])
    changed_wording["label"] = "改写后的业务标签（推荐）"
    assert (
        langgraph_workflow._single_authority_decision_option_record(
            slot=slot,
            time_spec=TARGET_RANGE,
            option=changed_wording,
        )["option_id"]
        == records[0]["option_id"]
    )


def test_daily_comparison_baseline_keeps_case_b_option_contract() -> None:
    slot = next(
        dict(item)
        for item in langgraph_workflow._single_authority_ambiguity_slot_catalog()
        if item["slot_id"] == "comparison_baseline"
    )
    output = {
        "question": "目标日期应采用哪个比较基线？",
        "options": [
            {
                "value_ref": "previous_day",
                "label": "跟前一天比较（推荐）",
                "description": "观察相邻日期变化。",
                "recommended": True,
            },
            {
                "value_ref": "same_weekday_last_week",
                "label": "跟上周同日比较",
                "description": "控制星期位置差异。",
                "recommended": False,
            },
        ],
        "recommendation_reason": "前一天最贴近目标日。",
        "status_message": "等待确认日级比较基线。",
    }
    options = langgraph_workflow._validate_single_authority_clarification_output(
        output,
        slot=slot,
        time_spec={"kind": "date", "target": "2026-06-19"},
        required_recommended_value_ref="previous_day",
        required_recommended_label="跟前一天比较（推荐）",
    )

    record = langgraph_workflow._single_authority_decision_option_record(
        slot=slot,
        time_spec={"kind": "date", "target": "2026-06-19"},
        option=options[0],
    )

    assert record["option_id"] == "comparison_baseline.previous_day"
    assert record["typed_value"] == {"baseline_id": "previous_day"}


def test_calendar_partition_is_admitted_as_a_complete_window_option() -> None:
    time_spec = {
        "kind": "date_range",
        "start": "2024-01-01",
        "end": "2026-12-31",
    }
    output = {
        "question": "需要按哪种日历分区比较？",
        "options": [
            {
                "value_ref": "prior_period",
                "typed_value": {
                    "kind": "calendar_partition",
                    "baseline_class": "prior_period",
                    "period_grain": "year",
                    "partition_field": "quarter_of_year",
                    "target_members": ["Q2"],
                    "baseline_members": ["Q1"],
                    "aggregation": "mean_of_complete_days",
                },
                "label": "按年内相邻季度比较（推荐）",
                "description": "在完整评估范围内比较季度位置。",
                "recommended": True,
            },
            {
                "value_ref": "custom_control_window",
                "typed_value": {
                    "kind": "calendar_partition",
                    "baseline_class": "custom_control_window",
                    "period_grain": "year",
                    "partition_field": "quarter_of_year",
                    "target_members": ["Q2"],
                    "baseline_members": ["Q4"],
                    "aggregation": "mean_of_complete_days",
                },
                "label": "按指定季度位置比较",
                "description": "使用已明确的季度位置作为控制组。",
                "recommended": False,
            },
        ],
        "recommendation_reason": "相邻季度具有清晰的日历解释。",
        "status_message": "等待确认日历比较方式。",
    }

    options = langgraph_workflow._validate_single_authority_clarification_output(
        output,
        slot=_comparison_window_slot(),
        time_spec=time_spec,
        required_recommended_value_ref="",
        required_recommended_label="",
    )

    assert options[0]["typed_value"]["kind"] == "calendar_partition"
    assert options[0]["typed_value"]["target_members"] == ["Q2"]


@pytest.mark.parametrize(
    ("time_spec", "partition_field", "target_members", "baseline_members"),
    (
        (
            {"kind": "date_range", "start": "2026-01-11", "end": "2026-01-20"},
            "month_phase",
            ["start"],
            ["mid"],
        ),
        (
            {"kind": "date_range", "start": "2026-01-01", "end": "2026-01-10"},
            "month_phase",
            ["start"],
            ["mid"],
        ),
        (
            {"kind": "date_range", "start": "2026-06-01", "end": "2026-06-01"},
            "iso_weekday",
            [1],
            [2],
        ),
    ),
)
def test_calendar_partition_rejects_a_deterministically_empty_side(
    time_spec,
    partition_field,
    target_members,
    baseline_members,
) -> None:
    period_grain = "month" if partition_field == "month_phase" else "week"
    baseline_class = (
        "same_month_phase"
        if partition_field == "month_phase"
        else "custom_control_window"
    )

    with pytest.raises(
        TemporalComparisonContractError,
        match="temporal_comparison_spec_invalid",
    ):
        validate_comparison_spec(
            {
                "kind": "calendar_partition",
                "baseline_class": baseline_class,
                "period_grain": period_grain,
                "partition_field": partition_field,
                "target_members": target_members,
                "baseline_members": baseline_members,
                "aggregation": "mean_of_complete_days",
            },
            time_spec=time_spec,
        )


def test_temporal_authority_deep_freezes_calendar_members() -> None:
    authority = resolve_effective_comparison(
        time_spec={
            "kind": "date_range",
            "start": "2024-01-01",
            "end": "2026-12-31",
        },
        comparison_spec={
            "kind": "calendar_partition",
            "baseline_class": "prior_period",
            "period_grain": "year",
            "partition_field": "quarter_of_year",
            "target_members": ["Q2"],
            "baseline_members": ["Q1"],
            "aggregation": "mean_of_complete_days",
        },
        decision_ledger=DecisionLedger(),
        require_physical_baseline=False,
    )

    assert authority.calendar_partition is not None
    assert authority.calendar_partition["target_members"] == ("Q2",)
    assert authority.intent_comparison_spec["target_members"] == ("Q2",)
    assert authority.effective_comparison_spec["baseline_members"] == ("Q1",)
    with pytest.raises(AttributeError):
        authority.calendar_partition["target_members"].append("Q3")
    assert authority.to_dict()["calendar_partition"]["target_members"] == ["Q2"]


def test_comparison_window_rejects_incomplete_provider_typed_value() -> None:
    output = {
        "question": "采用哪个比较窗口？",
        "options": [
            {
                "value_ref": "prior_period",
                "typed_value": {
                    "kind": "fixed_window",
                    "baseline_class": "prior_period",
                },
                "label": "上一周期（推荐）",
                "description": "比较上一完整周期。",
                "recommended": True,
            },
            {
                "value_ref": "same_period_last_year",
                "typed_value": {
                    "kind": "fixed_window",
                    "baseline_class": "same_period_last_year",
                    "baseline_start": "2025-04-01",
                    "baseline_end": "2025-06-30",
                    "aggregation": "sum_of_complete_days",
                },
                "label": "去年同期",
                "description": "比较去年同期。",
                "recommended": False,
            },
        ],
        "recommendation_reason": "上一周期更接近当前窗口。",
        "status_message": "等待确认。",
    }

    with pytest.raises(
        LLMOutputError,
        match="single_authority_clarification_typed_value_invalid",
    ):
        langgraph_workflow._validate_single_authority_clarification_output(
            output,
            slot=_comparison_window_slot(),
            time_spec=TARGET_RANGE,
            required_recommended_value_ref="",
            required_recommended_label="",
        )


def test_time_structure_owns_the_missing_comparison_slot_without_duplicates() -> None:
    valid = _intent(time_spec=TARGET_RANGE, slot_id="comparison_window")
    assert valid.comparison_spec == {
        "kind": "decision_slot",
        "slot_id": "comparison_window",
    }

    with pytest.raises(
        SingleAuthorityContractError,
        match="intent_revision_comparison_spec_invalid",
    ):
        _intent(time_spec=TARGET_RANGE, slot_id="comparison_baseline")
    with pytest.raises(
        SingleAuthorityContractError,
        match="intent_revision_comparison_spec_invalid",
    ):
        _intent(
            time_spec={"kind": "date", "target": "2026-06-19"},
            slot_id="comparison_window",
        )

    event_slot = next(
        slot
        for slot in langgraph_workflow._single_authority_ambiguity_slot_catalog()
        if slot["slot_id"] == "event_relative_window"
    )
    with pytest.raises(
        SingleAuthorityContractError,
        match="intent_revision_comparison_authority_invalid",
    ):
        _intent(
            time_spec=TARGET_RANGE,
            slot_id="comparison_window",
            extra_slots=(
                {
                    "slot_id": "event_relative_window",
                    "slot_kind": event_slot["slot_kind"],
                    "materiality": "material",
                    "status": "unresolved",
                    "question": "请确认事件窗口。",
                    "allowed_value_refs": [],
                },
            ),
        )


def test_free_text_binding_can_select_a_typed_comparison_window_option() -> None:
    intent = _intent(time_spec=TARGET_RANGE, slot_id="comparison_window")
    typed_value = {
        "kind": "fixed_window",
        "baseline_class": "prior_period",
        "baseline_start": "2026-01-01",
        "baseline_end": "2026-03-31",
        "aggregation": "sum_of_complete_days",
    }
    option = {
        "slot_id": "comparison_window",
        "option_id": temporal_decision_option_id(
            slot_id="comparison_window",
            value=typed_value,
            time_spec=intent.time_spec,
        ),
        "typed_value": typed_value,
        "display_label": "上一完整周期（推荐）",
        "display_description": "比较相邻完整周期。",
        "recommended": True,
    }
    candidate = {
        "binding_kind": "fill_current_slot",
        "slot_id": "comparison_window",
        "value_ref": "prior_period",
        "target_refs": [],
        "affected_binding_fields": [],
        "replacement_user_text": "",
        "status_message": "已选择上一完整周期。",
    }

    _validated_single_authority_decision_binding(
        candidate,
        active_revision=intent,
        ledger=DecisionLedger(),
        options=(option,),
        challenge_target_refs=frozenset({intent.intent_revision_id}),
    )


def test_prompt_contract_exposes_structural_slot_and_complete_typed_options() -> None:
    prompt = build_prompt("single_authority_intent", {"contract": "test"})
    clarification = build_prompt(
        "single_authority_clarification",
        {"contract": "test"},
    )
    prompt_text = "\n".join(message["content"] for message in prompt.messages)
    clarification_text = "\n".join(
        message["content"] for message in clarification.messages
    )

    assert prompt.prompt_version.endswith(".v9")
    assert "quarter-to-quarter" in prompt_text
    assert "comparison_baseline for kind date" in prompt_text
    assert "comparison_window for kind date_range" in prompt_text
    assert (
        "Do not choose between those slots from business-question keywords"
        in prompt_text
    )
    assert "typed_value must be one complete fixed_window or" in clarification_text
    assert "runtime supplies a separate free-text outlet" in clarification_text
    assert _comparison_window_slot()["time_spec_kinds"] == ["date_range"]


def test_effective_temporal_comparison_from_dict_is_strict_and_content_addressed() -> (
    None
):
    intent = _intent(time_spec=TARGET_RANGE, slot_id="comparison_window")
    typed_value = {
        "kind": "fixed_window",
        "baseline_class": "prior_period",
        "baseline_start": "2026-01-01",
        "baseline_end": "2026-03-31",
        "aggregation": "sum_of_complete_days",
    }
    decision = DecisionRecord.create(
        intent_revision_id=intent.intent_revision_id,
        slot_id="comparison_window",
        value=typed_value,
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=("resolved_window_refs",),
        option_id=temporal_decision_option_id(
            slot_id="comparison_window",
            value=typed_value,
            time_spec=intent.time_spec,
        ),
    )
    authority = resolve_effective_comparison(
        time_spec=intent.time_spec,
        comparison_spec=intent.comparison_spec,
        decision_ledger=DecisionLedger().append(decision),
        require_physical_baseline=True,
    )

    assert EffectiveTemporalComparison.from_dict(authority.to_dict()) == authority

    extra = authority.to_dict()
    extra["debug"] = True
    with pytest.raises(
        TemporalComparisonContractError,
        match="temporal_authority_shape_invalid",
    ):
        EffectiveTemporalComparison.from_dict(extra)

    nested = authority.to_dict()
    nested["target_window"]["debug"] = True
    with pytest.raises(
        TemporalComparisonContractError,
        match="temporal_authority_shape_invalid",
    ):
        EffectiveTemporalComparison.from_dict(nested)

    tampered = authority.to_dict()
    tampered["content_digest"] = "0" * 64
    tampered["authority_ref"] = "temporal-comparison:sha256:" + "0" * 64
    with pytest.raises(
        TemporalComparisonContractError,
        match="temporal_authority_integrity_invalid",
    ):
        EffectiveTemporalComparison.from_dict(tampered)

    rebound = authority.to_dict()
    rebound["time_spec"] = {
        "kind": "date_range",
        "start": "2026-04-02",
        "end": "2026-06-30",
    }
    rebound.pop("content_digest")
    rebound.pop("authority_ref")
    rebound_digest = canonical_digest(rebound)
    rebound["content_digest"] = rebound_digest
    rebound["authority_ref"] = "temporal-comparison:sha256:" + rebound_digest
    with pytest.raises(
        TemporalComparisonContractError,
        match="temporal_authority_shape_invalid",
    ):
        EffectiveTemporalComparison.from_dict(rebound)


def test_effective_temporal_comparison_from_dict_covers_every_authority_mode() -> None:
    daily_decision = DecisionRecord.create(
        intent_revision_id="intent-daily-roundtrip",
        slot_id="comparison_baseline",
        value={"baseline_id": "previous_day"},
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=("resolved_window_refs",),
        option_id="comparison_baseline.previous_day",
    )
    authorities = (
        resolve_effective_comparison(
            time_spec={"kind": "date", "target": "2026-06-19"},
            comparison_spec={"kind": "none"},
            decision_ledger=DecisionLedger(),
            require_physical_baseline=False,
        ),
        resolve_effective_comparison(
            time_spec={"kind": "date", "target": "2026-06-19"},
            comparison_spec={
                "kind": "decision_slot",
                "slot_id": "comparison_baseline",
            },
            decision_ledger=DecisionLedger().append(daily_decision),
            require_physical_baseline=True,
        ),
        resolve_effective_comparison(
            time_spec={
                "kind": "date_range",
                "start": "2024-01-01",
                "end": "2026-12-31",
            },
            comparison_spec={
                "kind": "calendar_partition",
                "baseline_class": "prior_period",
                "period_grain": "year",
                "partition_field": "quarter_of_year",
                "target_members": ["Q2"],
                "baseline_members": ["Q1"],
                "aggregation": "mean_of_complete_days",
            },
            decision_ledger=DecisionLedger(),
            require_physical_baseline=False,
        ),
        resolve_effective_comparison(
            time_spec={
                "kind": "date_range",
                "start": "2026-06-16",
                "end": "2026-06-30",
            },
            comparison_spec={
                "kind": "event_relative_window",
                "event_ref": "business-event:campaign-june-2026",
                "target_start": "2026-06-16",
                "target_end": "2026-06-30",
                "baseline_start": "2026-06-01",
                "baseline_end": "2026-06-15",
                "aggregation": "sum_of_complete_days",
            },
            decision_ledger=DecisionLedger(),
            require_physical_baseline=True,
        ),
        resolve_effective_comparison(
            time_spec=TARGET_RANGE,
            comparison_spec={
                "kind": "decision_slot",
                "slot_id": "comparison_window",
            },
            decision_ledger=DecisionLedger(),
            require_physical_baseline=False,
        ),
        resolve_effective_comparison(
            time_spec={"kind": "custom", "expression": "活动后"},
            comparison_spec={
                "kind": "decision_slot",
                "slot_id": "event_relative_window",
            },
            decision_ledger=DecisionLedger(),
            require_physical_baseline=False,
        ),
    )

    assert {authority.mode for authority in authorities} == {
        "target_only",
        "window_pair",
        "calendar_partition",
        "event_relative",
        "unresolved",
    }
    assert all(
        EffectiveTemporalComparison.from_dict(authority.to_dict()) == authority
        for authority in authorities
    )
    assert authorities[-1].time_spec == {
        "kind": "custom",
        "expression": "活动后",
    }
    assert authorities[-1].target_window.boundary == "unresolved"
