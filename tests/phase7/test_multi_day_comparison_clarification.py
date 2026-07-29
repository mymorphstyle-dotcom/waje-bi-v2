from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from bi_agent.conversation.agent_core import (
    _validated_single_authority_decision_binding,
)
from bi_agent.conversation.postgres_store import (
    _decision_option_value_ref_is_admitted,
)
from bi_agent.runtime import langgraph_workflow
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.llm_client import LLMOutputError
from bi_agent.runtime.llm_prompts import (
    SINGLE_AUTHORITY_PROMPT_VERSION,
    build_prompt,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
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
    normalize_temporal_decision_value,
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


def _comparison_interpretation_slot() -> dict[str, object]:
    return next(
        dict(slot)
        for slot in langgraph_workflow._single_authority_ambiguity_slot_catalog()
        if slot["slot_id"] == "comparison_interpretation"
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


def test_calendar_partition_reports_invalid_baseline_class_for_output_repair() -> None:
    with pytest.raises(
        TemporalComparisonContractError,
        match="temporal_calendar_partition_baseline_class_invalid",
    ):
        validate_comparison_spec(
            {
                "kind": "calendar_partition",
                "baseline_class": "custom_control_window",
                "period_grain": "month",
                "partition_field": "month_phase",
                "target_members": ["start"],
                "baseline_members": ["end"],
                "aggregation": "sum_of_complete_days",
                "member_definitions": [
                    {"member": "start", "day_start": 1, "day_end": 5},
                    {"member": "mid", "day_start": 6, "day_end": 25},
                    {"member": "end", "day_start": 26, "day_end": 31},
                ],
            },
            time_spec={
                "kind": "date_range",
                "start": "2024-01-01",
                "end": "2026-05-31",
            },
        )


def test_provider_adapter_removes_target_metric_from_requested_factor_refs() -> None:
    binding = {
        "target_metric_refs": ["paid_amount"],
        "requested_factor_refs": [
            "paid_amount",
            "paid_users",
            "paid_amount_per_paid_user",
        ],
        "time_spec": {"kind": "date", "target": "2026-05-20"},
        "comparison_spec": {"kind": "none"},
    }

    normalized = langgraph_workflow._normalize_provider_intent_binding(binding)

    assert normalized["requested_factor_refs"] == [
        "paid_users",
        "paid_amount_per_paid_user",
    ]
    assert binding["requested_factor_refs"][0] == "paid_amount"


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
        business_summary="你希望分析指定时间范围内付费金额的变化。",
        goal_bindings=({"goal_id": "explain_change", "role": "primary"},),
        target_metric_refs=("paid_amount",),
        scope={"scope_type": "full_sample", "filters": []},
        time_spec=time_spec,
        comparison_spec={"kind": "decision_slot", "slot_id": slot_id},
        direction_premise="unknown",
        requested_factor_refs=(),
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
        schema_version="intent-revision.v3",
        prompt_version=SINGLE_AUTHORITY_PROMPT_VERSION,
        model_version="provider-test",
        known_ambiguity_value_refs={
            str(value_ref)
            for slot in catalog.values()
            for value_ref in slot["allowed_value_refs"]
        },
        known_ambiguity_slots=catalog,
    )


def _comparison_grounding(
    comparison_spec: dict[str, object],
) -> dict[str, object]:
    kind = comparison_spec["kind"]
    if kind == "none":
        presence = "absent"
        relation = "none"
    elif kind == "decision_slot":
        presence = "implicit"
        relation = "unresolved"
    elif kind == "event_relative_window":
        presence = "implicit"
        relation = "event_relative"
    else:
        presence = "implicit"
        relation = comparison_spec["baseline_class"]
    return {
        "comparison_presence": presence,
        "baseline_relation": relation,
        "target_member_refs": list(comparison_spec.get("target_members", [])),
        "baseline_member_refs": list(comparison_spec.get("baseline_members", [])),
        "target_text": None,
        "baseline_text": None,
    }


def test_provider_intent_accepts_sql_interpretation_clarification_slot() -> None:
    intent = _intent(
        time_spec=TARGET_RANGE,
        slot_id="comparison_interpretation",
    )
    payload = intent.to_dict()
    binding_fields = {
        "goal_bindings",
        "target_metric_refs",
        "scope",
        "time_spec",
        "comparison_spec",
        "direction_premise",
        "requested_analysis_axes",
        "requested_factor_refs",
        "desired_decisions",
        "ambiguity_slots",
        "source_spans",
    }
    validated = langgraph_workflow._validated_single_authority_intent_output(
        {
            "intent_binding": {
                key: payload[key]
                for key in binding_fields
            },
            "comparison_grounding": _comparison_grounding(
                payload["comparison_spec"]
            ),
            "business_summary": payload["business_summary"],
        },
        run_attempt_id="run-provider-comparison-interpretation",
        question=payload["original_user_text"],
        registry=RuntimeContractRegistry.from_path(
            CANONICAL_RUNTIME_BINDINGS_PATH
        ),
        prompt_version=SINGLE_AUTHORITY_PROMPT_VERSION,
        model_version="provider-test",
        supersedes_intent_revision_id=None,
    )

    assert validated.comparison_spec == {
        "kind": "decision_slot",
        "slot_id": "comparison_interpretation",
    }
    assert validated.ambiguity_slots[0]["allowed_value_refs"] == (
        "interpretation_1",
        "interpretation_2",
        "interpretation_3",
    )


def test_provider_intent_rejects_goal_without_its_required_temporal_authority() -> None:
    intent = _intent(
        time_spec=TARGET_RANGE,
        slot_id="comparison_interpretation",
    )
    payload = intent.to_dict()
    binding_fields = {
        "goal_bindings",
        "target_metric_refs",
        "scope",
        "time_spec",
        "comparison_spec",
        "direction_premise",
        "requested_analysis_axes",
        "requested_factor_refs",
        "desired_decisions",
        "ambiguity_slots",
        "source_spans",
    }
    provider_binding = {key: payload[key] for key in binding_fields}
    provider_binding["comparison_spec"] = {"kind": "none"}
    provider_binding["ambiguity_slots"] = []

    with pytest.raises(
        LLMOutputError,
        match="single_authority_intent_physical_baseline_binding_invalid",
    ):
        langgraph_workflow._validated_single_authority_intent_output(
            {
                "intent_binding": provider_binding,
                "comparison_grounding": _comparison_grounding(
                    provider_binding["comparison_spec"]
                ),
                "business_summary": payload["business_summary"],
            },
            run_attempt_id="run-provider-missing-baseline",
            question=payload["original_user_text"],
            registry=RuntimeContractRegistry.from_path(
                CANONICAL_RUNTIME_BINDINGS_PATH
            ),
            prompt_version=SINGLE_AUTHORITY_PROMPT_VERSION,
            model_version="provider-test",
            supersedes_intent_revision_id=None,
        )


def test_provider_intent_rejects_requested_comparison_axis_without_comparison() -> None:
    intent = _intent(
        time_spec=TARGET_RANGE,
        slot_id="comparison_interpretation",
    )
    payload = intent.to_dict()
    binding_fields = {
        "goal_bindings",
        "target_metric_refs",
        "scope",
        "time_spec",
        "comparison_spec",
        "direction_premise",
        "requested_analysis_axes",
        "requested_factor_refs",
        "desired_decisions",
        "ambiguity_slots",
        "source_spans",
    }
    provider_binding = {key: payload[key] for key in binding_fields}
    provider_binding["goal_bindings"] = [
        {"goal_id": "pattern_explanation", "role": "primary"}
    ]
    provider_binding["comparison_spec"] = {"kind": "none"}
    provider_binding["requested_analysis_axes"] = ["change_validation"]
    provider_binding["ambiguity_slots"] = []

    with pytest.raises(
        LLMOutputError,
        match="single_authority_intent_requested_comparison_binding_invalid",
    ):
        langgraph_workflow._validated_single_authority_intent_output(
            {
                "intent_binding": provider_binding,
                "comparison_grounding": _comparison_grounding(
                    provider_binding["comparison_spec"]
                ),
                "business_summary": payload["business_summary"],
            },
            run_attempt_id="run-provider-requested-comparison-missing",
            question=payload["original_user_text"],
            registry=RuntimeContractRegistry.from_path(
                CANONICAL_RUNTIME_BINDINGS_PATH
            ),
            prompt_version=SINGLE_AUTHORITY_PROMPT_VERSION,
            model_version="provider-test",
            supersedes_intent_revision_id=None,
        )


def test_provider_intent_rejects_directional_premise_without_comparison() -> None:
    intent = _intent(
        time_spec=TARGET_RANGE,
        slot_id="comparison_interpretation",
    )
    payload = intent.to_dict()
    binding_fields = {
        "goal_bindings",
        "target_metric_refs",
        "scope",
        "time_spec",
        "comparison_spec",
        "direction_premise",
        "requested_analysis_axes",
        "requested_factor_refs",
        "desired_decisions",
        "ambiguity_slots",
        "source_spans",
    }
    provider_binding = {key: payload[key] for key in binding_fields}
    provider_binding["goal_bindings"] = [
        {"goal_id": "pattern_explanation", "role": "primary"}
    ]
    provider_binding["comparison_spec"] = {"kind": "none"}
    provider_binding["direction_premise"] = "user_hypothesis_positive"
    provider_binding["requested_analysis_axes"] = ["time_context"]
    provider_binding["ambiguity_slots"] = []

    with pytest.raises(
        LLMOutputError,
        match="single_authority_intent_directional_comparison_binding_invalid",
    ):
        langgraph_workflow._validated_single_authority_intent_output(
            {
                "intent_binding": provider_binding,
                "comparison_grounding": _comparison_grounding(
                    provider_binding["comparison_spec"]
                ),
                "business_summary": payload["business_summary"],
            },
            run_attempt_id="run-provider-directional-comparison-missing",
            question=payload["original_user_text"],
            registry=RuntimeContractRegistry.from_path(
                CANONICAL_RUNTIME_BINDINGS_PATH
            ),
            prompt_version=SINGLE_AUTHORITY_PROMPT_VERSION,
            model_version="provider-test",
            supersedes_intent_revision_id=None,
        )


def _prior_month_phase_intent_output(
    *,
    comparison_baseline_class: str,
    grounding_baseline_relation: str,
) -> tuple[str, dict[str, object]]:
    question = (
        "分析2024年1月至2026年5月全量样本中，每月月初是否绝大部分"
        "比上个月月末金额高啊？有哪些驱动因子？有哪些例外情况？"
    )
    comparison_spec = {
        "kind": "calendar_partition",
        "baseline_class": comparison_baseline_class,
        "period_grain": "month",
        "partition_field": "month_phase",
        "target_members": ["start"],
        "baseline_members": ["end"],
        "aggregation": "sum_of_complete_days",
        "member_definitions": [
            {"member": "start", "day_start": 1, "day_end": 5},
            {"member": "mid", "day_start": 6, "day_end": 24},
            {"member": "end", "day_start": 25, "day_end": 31},
        ],
    }
    return question, {
        "intent_binding": {
            "goal_bindings": [
                {"goal_id": "pattern_explanation", "role": "primary"}
            ],
            "target_metric_refs": ["paid_amount"],
            "scope": {"scope_type": "full_sample", "filters": []},
            "time_spec": {
                "kind": "date_range",
                "start": "2024-01-01",
                "end": "2026-05-31",
            },
            "comparison_spec": comparison_spec,
            "direction_premise": "user_hypothesis_positive",
            "requested_analysis_axes": [],
            "requested_factor_refs": [],
            "desired_decisions": [],
            "ambiguity_slots": [],
            "source_spans": [
                {
                    "field": "original_user_text",
                    "start": 0,
                    "end": len(question),
                    "text": question,
                }
            ],
        },
        "comparison_grounding": {
            "comparison_presence": "explicit",
            "baseline_relation": grounding_baseline_relation,
            "target_member_refs": ["start"],
            "baseline_member_refs": ["end"],
            "target_text": "每月月初",
            "baseline_text": "上个月月末",
        },
        "business_summary": (
            "分析全量样本中每月月初相对上个月月末的金额表现、"
            "驱动因素和例外情况。"
        ),
    }


def test_provider_intent_rejects_period_relation_that_conflicts_with_grounding() -> (
    None
):
    question, output = _prior_month_phase_intent_output(
        comparison_baseline_class="same_month_phase",
        grounding_baseline_relation="prior_period",
    )

    with pytest.raises(
        LLMOutputError,
        match="single_authority_intent_comparison_grounding_invalid",
    ) as error:
        langgraph_workflow._validated_single_authority_intent_output(
            output,
            run_attempt_id="run-provider-period-relation-mismatch",
            question=question,
            registry=RuntimeContractRegistry.from_path(
                CANONICAL_RUNTIME_BINDINGS_PATH
            ),
            prompt_version=SINGLE_AUTHORITY_PROMPT_VERSION,
            model_version="provider-test",
            supersedes_intent_revision_id=None,
        )

    assert error.value.retryable is True
    assert error.value.repair_contract["authority"] == "original_user_text"


def test_provider_intent_accepts_grounded_prior_period_month_phase() -> None:
    question, output = _prior_month_phase_intent_output(
        comparison_baseline_class="prior_period",
        grounding_baseline_relation="prior_period",
    )

    revision = langgraph_workflow._validated_single_authority_intent_output(
        output,
        run_attempt_id="run-provider-period-relation-aligned",
        question=question,
        registry=RuntimeContractRegistry.from_path(
            CANONICAL_RUNTIME_BINDINGS_PATH
        ),
        prompt_version=SINGLE_AUTHORITY_PROMPT_VERSION,
        model_version="provider-test",
        supersedes_intent_revision_id=None,
    )

    assert revision.comparison_spec["baseline_class"] == "prior_period"


def test_provider_repair_notice_uses_business_language() -> None:
    notices = langgraph_workflow._provider_repair_notices(
        task="single_authority_intent",
        audit={
            "attempt_count": 2,
            "attempt_failures": [
                {
                    "failure_code": (
                        "single_authority_intent_"
                        "comparison_grounding_invalid"
                    )
                }
            ],
        },
    )

    assert notices == (
        "检测到生成的日期比较口径没有完整覆盖业务问题，"
        "已重新理解时间关系并核验修正后的分析口径。",
    )


def test_narrative_material_repair_notice_uses_business_language() -> None:
    result = SimpleNamespace(
        narrative_workflow=SimpleNamespace(
            provider_audits=(
                SimpleNamespace(
                    attempt_count=1,
                    audit_payload={
                        "provider_transport": {
                            "material_transport_repair_mode": (
                                "required-fact-claim-complete.v1"
                            )
                        }
                    }
                ),
            )
        )
    )

    assert langgraph_workflow._narrative_repair_notices(result) == (
        "生成最终回答时检测到分析材料超出单次处理范围，"
        "已保留全部结论与必需事实，重新组织证据并完成核验。",
    )


def test_provider_intent_reports_noncontiguous_month_phase_ranges() -> None:
    intent = _intent(
        time_spec=TARGET_RANGE,
        slot_id="comparison_interpretation",
    )
    payload = intent.to_dict()
    binding_fields = {
        "goal_bindings",
        "target_metric_refs",
        "scope",
        "time_spec",
        "comparison_spec",
        "direction_premise",
        "requested_analysis_axes",
        "requested_factor_refs",
        "desired_decisions",
        "ambiguity_slots",
        "source_spans",
    }
    provider_binding = {key: payload[key] for key in binding_fields}
    provider_binding["goal_bindings"] = [
        {"goal_id": "pattern_explanation", "role": "primary"}
    ]
    provider_binding["comparison_spec"] = {
        "kind": "calendar_partition",
        "baseline_class": "prior_period",
        "period_grain": "month",
        "partition_field": "month_phase",
        "target_members": ["start"],
        "baseline_members": ["end"],
        "aggregation": "sum_of_complete_days",
        "member_definitions": [
            {"member": "start", "day_start": 1, "day_end": 5},
            {"member": "mid", "day_start": 11, "day_end": 20},
            {"member": "end", "day_start": 26, "day_end": 31},
        ],
    }

    with pytest.raises(
        LLMOutputError,
        match=(
            "intent_revision_comparison_spec_invalid:"
            "temporal_month_phase_member_ranges_not_contiguous"
        ),
    ):
        langgraph_workflow._validated_single_authority_intent_output(
            {
                "intent_binding": provider_binding,
                "comparison_grounding": _comparison_grounding(
                    provider_binding["comparison_spec"]
                ),
                "business_summary": payload["business_summary"],
            },
            run_attempt_id="run-provider-noncontiguous-month-phase",
            question=payload["original_user_text"],
            registry=RuntimeContractRegistry.from_path(
                CANONICAL_RUNTIME_BINDINGS_PATH
            ),
            prompt_version=SINGLE_AUTHORITY_PROMPT_VERSION,
            model_version="provider-test",
            supersedes_intent_revision_id=None,
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


def test_sql_affecting_comparison_interpretations_are_distinct_typed_options() -> (
    None
):
    slot = _comparison_interpretation_slot()
    time_spec = {
        "kind": "date_range",
        "start": "2024-01-01",
        "end": "2026-05-31",
    }
    output = {
        "question": "月初、月中和月末按哪组日期及汇总方式比较？",
        "options": [
            {
                "value_ref": "interpretation_1",
                "typed_value": {
                    "kind": "calendar_partition",
                    "baseline_class": "same_month_phase",
                    "period_grain": "month",
                    "partition_field": "month_phase",
                    "target_members": ["start"],
                    "baseline_members": ["mid", "end"],
                    "aggregation": "sum_of_complete_days",
                    "member_definitions": [
                        {"member": "start", "day_start": 1, "day_end": 10},
                        {"member": "mid", "day_start": 11, "day_end": 20},
                        {"member": "end", "day_start": 21, "day_end": 31},
                    ],
                },
                "label": "1—10日、11—20日、21日至月末，比较阶段总额（推荐）",
                "description": "适合判断三个阶段各自贡献的付费总额。",
                "recommended": True,
            },
            {
                "value_ref": "interpretation_2",
                "typed_value": {
                    "kind": "calendar_partition",
                    "baseline_class": "same_month_phase",
                    "period_grain": "month",
                    "partition_field": "month_phase",
                    "target_members": ["start"],
                    "baseline_members": ["mid", "end"],
                    "aggregation": "mean_of_complete_days",
                    "member_definitions": [
                        {"member": "start", "day_start": 1, "day_end": 7},
                        {"member": "mid", "day_start": 8, "day_end": 21},
                        {"member": "end", "day_start": 22, "day_end": 31},
                    ],
                },
                "label": "1—7日、8—21日、22日至月末，比较日均金额",
                "description": "适合控制三个阶段天数不同带来的影响。",
                "recommended": False,
            },
        ],
        "recommendation_reason": "十天分段是更常见且便于复核的业务解释。",
        "status_message": "等待确认月内阶段口径。",
    }

    normalized = langgraph_workflow._validate_single_authority_clarification_output(
        output,
        slot=slot,
        time_spec=time_spec,
        required_recommended_value_ref="",
        required_recommended_label="",
    )
    records = [
        langgraph_workflow._single_authority_decision_option_record(
            slot=slot,
            time_spec=time_spec,
            option=option,
        )
        for option in normalized
    ]

    assert len({record["option_id"] for record in records}) == 2
    assert all(
        record["option_id"].startswith(
            "comparison_interpretation.interpretation_"
        )
        for record in records
    )
    _, content_value_ref = normalize_temporal_decision_value(
        slot_id="comparison_interpretation",
        value=records[0]["typed_value"],
        time_spec=time_spec,
    )
    assert content_value_ref not in slot["allowed_value_refs"]
    assert _decision_option_value_ref_is_admitted(
        slot=slot,
        normalized_value_ref=content_value_ref,
        option_count=len(records),
    )
    assert not _decision_option_value_ref_is_admitted(
        slot=slot,
        normalized_value_ref=content_value_ref,
        option_count=len(slot["allowed_value_refs"]) + 1,
    )
    intent = _intent(
        time_spec=time_spec,
        slot_id="comparison_interpretation",
    )
    decision = DecisionRecord.create(
        intent_revision_id=intent.intent_revision_id,
        slot_id="comparison_interpretation",
        value=records[0]["typed_value"],
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=("baseline_refs", "resolved_window_refs"),
        option_id=records[0]["option_id"],
    )
    authority = resolve_effective_comparison(
        time_spec=intent.time_spec,
        comparison_spec=intent.comparison_spec,
        decision_ledger=DecisionLedger().append(decision),
        require_physical_baseline=False,
    )

    assert authority.mode == "calendar_partition"
    assert authority.calendar_partition["member_definitions"][0]["day_end"] == 10
    assert authority.calendar_partition["aggregation"] == "sum_of_complete_days"


def test_month_phase_clarification_keeps_boundaries_and_aggregation_atomic() -> None:
    original_user_text = "比较每个月月初、月中和月末付费金额。"
    time_spec = {
        "kind": "date_range",
        "start": "2024-01-01",
        "end": "2026-05-31",
    }
    comparison_spec = {
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
    }
    catalog = {
        str(item["slot_id"]): item
        for item in langgraph_workflow._single_authority_ambiguity_slot_catalog()
    }
    selected_slots = [
        catalog["month_phase_definition"],
        catalog["phase_aggregation"],
    ]
    ambiguity_slots = tuple(
        {
            "slot_id": slot["slot_id"],
            "slot_kind": slot["slot_kind"],
            "materiality": slot["materiality"],
            "status": "unresolved",
            "question": (
                "月初、月中和月末分别包含哪些日期？"
                if slot["slot_id"] == "month_phase_definition"
                else "比较每个阶段的总额还是日均金额？"
            ),
            "allowed_value_refs": slot["allowed_value_refs"],
        }
        for slot in selected_slots
    )
    intent = IntentRevision.create(
        run_attempt_id="run-atomic-month-phase",
        original_user_text=original_user_text,
        business_summary=(
            "按1—10日、11—20日、21日至月末分组，推荐比较日均付费金额。"
        ),
        goal_bindings=({"goal_id": "pattern_explanation", "role": "primary"},),
        target_metric_refs=("paid_amount",),
        scope={"scope_type": "full_sample", "filters": []},
        time_spec=time_spec,
        comparison_spec=comparison_spec,
        direction_premise="unknown",
        requested_factor_refs=(),
        requested_analysis_axes=(),
        desired_decisions=(),
        ambiguity_slots=ambiguity_slots,
        source_spans=(
            {
                "field": "original_user_text",
                "start": 0,
                "end": len(original_user_text),
                "text": original_user_text,
            },
        ),
        schema_version="intent-revision.v3",
        prompt_version=SINGLE_AUTHORITY_PROMPT_VERSION,
        model_version="provider-test",
        known_ambiguity_value_refs={
            value_ref
            for slot in selected_slots
            for value_ref in slot["allowed_value_refs"]
        },
        known_ambiguity_slots=catalog,
    )
    slot_contracts = [
        langgraph_workflow._single_authority_clarification_slot_contract(
            slot=slot,
            comparison_spec=intent.comparison_spec,
        )
        for slot in intent.ambiguity_slots
    ]
    output = {
        "questions": [
            {
                "slot_id": "month_phase_definition",
                "question": "月初、月中和月末分别包含哪些日期？",
                "options": [
                    {
                        "value_ref": "definition_1",
                        "typed_value": {
                            "value_ref": "definition_1",
                            "member_definitions": comparison_spec[
                                "member_definitions"
                            ],
                        },
                        "label": "1—10日、11—20日、21日至月末（推荐）",
                        "description": "三个连续阶段覆盖整个月。",
                        "recommended": True,
                    },
                    {
                        "value_ref": "definition_2",
                        "typed_value": {
                            "value_ref": "definition_2",
                            "member_definitions": [
                                {
                                    "member": "start",
                                    "day_start": 1,
                                    "day_end": 7,
                                },
                                {
                                    "member": "mid",
                                    "day_start": 8,
                                    "day_end": 21,
                                },
                                {
                                    "member": "end",
                                    "day_start": 22,
                                    "day_end": 31,
                                },
                            ],
                        },
                        "label": "1—7日、8—21日、22日至月末",
                        "description": "按周长度划分月初和月末。",
                        "recommended": False,
                    },
                ],
                "recommendation_reason": "十天分段便于逐月复核。",
            },
            {
                "slot_id": "phase_aggregation",
                "question": "比较每个阶段的总额还是日均金额？",
                "options": [
                    {
                        "value_ref": "sum_of_complete_days",
                        "typed_value": {
                            "aggregation": "sum_of_complete_days",
                            "value_ref": "sum_of_complete_days",
                        },
                        "label": "比较阶段总额",
                        "description": "回答各阶段贡献了多少付费金额。",
                        "recommended": False,
                    },
                    {
                        "value_ref": "mean_of_complete_days",
                        "typed_value": {
                            "aggregation": "mean_of_complete_days",
                            "value_ref": "mean_of_complete_days",
                        },
                        "label": "比较日均金额（推荐）",
                        "description": "控制不同阶段天数差异。",
                        "recommended": True,
                    },
                ],
                "recommendation_reason": "日均口径便于公平比较。",
            },
        ],
        "status_message": "等待确认两个独立口径。",
    }
    projection = (
        langgraph_workflow._project_single_authority_clarification_output(
            output,
            slot_contracts=slot_contracts,
            time_spec=time_spec,
        )
    )
    output = dict(projection.output)
    assert projection.disposition == "accepted_normalized"
    assert [dict(item) for item in projection.mutations] == [
        {
            "path": (
                "questions[1].options[0].typed_value.value_ref"
            ),
            "action": "discard_surplus_field",
            "reason": "outside_consumer_contract",
        },
        {
            "path": (
                "questions[1].options[1].typed_value.value_ref"
            ),
            "action": "discard_surplus_field",
            "reason": "outside_consumer_contract",
        },
    ]
    questions = (
        langgraph_workflow._validate_single_authority_clarification_batch_output(
            output,
            slot_contracts=slot_contracts,
            time_spec=time_spec,
        )
    )
    assert [question["slot"]["slot_id"] for question in questions] == [
        "month_phase_definition",
        "phase_aggregation",
    ]
    records = [
        langgraph_workflow._single_authority_decision_option_record(
            slot=question["slot"],
            time_spec=time_spec,
            option=next(
                option
                for option in question["options"]
                if option["recommended"]
            ),
        )
        for question in questions
    ]
    ledger = DecisionLedger()
    for record in records:
        ledger = ledger.append(
            DecisionRecord.create(
                intent_revision_id=intent.intent_revision_id,
                slot_id=record["slot_id"],
                value=record["typed_value"],
                source="user",
                status="user_confirmed",
                materiality="material",
                affected_plan_fields=("resolved_window_refs",),
                option_id=record["option_id"],
            )
        )
    authority = resolve_effective_comparison(
        time_spec=time_spec,
        comparison_spec=intent.comparison_spec,
        decision_ledger=ledger,
        require_physical_baseline=False,
    )
    assert authority.source == "decision"
    assert authority.calendar_partition["aggregation"] == "mean_of_complete_days"
    assert authority.calendar_partition["member_definitions"][0]["day_end"] == 10


def test_month_phase_options_reject_distinct_refs_with_identical_boundaries() -> None:
    slot = next(
        dict(item)
        for item in langgraph_workflow._single_authority_ambiguity_slot_catalog()
        if item["slot_id"] == "month_phase_definition"
    )
    member_definitions = [
        {"member": "start", "day_start": 1, "day_end": 10},
        {"member": "mid", "day_start": 11, "day_end": 20},
        {"member": "end", "day_start": 21, "day_end": 31},
    ]
    output = {
        "question": "月初、月中和月末分别包含哪些日期？",
        "options": [
            {
                "value_ref": "definition_1",
                "typed_value": {
                    "value_ref": "definition_1",
                    "member_definitions": member_definitions,
                },
                "label": "1—10日、11—20日、21日至月末（推荐）",
                "description": "采用十天分段。",
                "recommended": True,
            },
            {
                "value_ref": "definition_2",
                "typed_value": {
                    "value_ref": "definition_2",
                    "member_definitions": deepcopy(member_definitions),
                },
                "label": "另一种分段",
                "description": "展示文案不同。",
                "recommended": False,
            },
        ],
        "recommendation_reason": "采用常用的十天分段。",
        "status_message": "等待确认。",
    }

    with pytest.raises(
        LLMOutputError,
        match="single_authority_clarification_typed_value_duplicate",
    ):
        langgraph_workflow._validate_single_authority_clarification_output(
            output,
            slot=slot,
            time_spec={
                "kind": "date_range",
                "start": "2024-01-01",
                "end": "2026-05-31",
            },
            required_recommended_value_ref="",
            required_recommended_label="",
        )


def test_month_phase_projection_rejects_conflicting_duplicate_identity() -> None:
    comparison_spec = {
        "kind": "calendar_partition",
        "baseline_class": "same_month_phase",
        "period_grain": "month",
        "partition_field": "month_phase",
        "target_members": ["start"],
        "baseline_members": ["mid", "end"],
        "aggregation": "sum_of_complete_days",
        "member_definitions": [
            {"member": "start", "day_start": 1, "day_end": 10},
            {"member": "mid", "day_start": 11, "day_end": 20},
            {"member": "end", "day_start": 21, "day_end": 31},
        ],
    }
    slot = next(
        dict(item)
        for item in langgraph_workflow._single_authority_ambiguity_slot_catalog()
        if item["slot_id"] == "phase_aggregation"
    )
    contract = langgraph_workflow._single_authority_clarification_slot_contract(
        slot=slot,
        comparison_spec=comparison_spec,
    )
    output = {
        "questions": [
            {
                "slot_id": "phase_aggregation",
                "question": "比较阶段总额还是日均金额？",
                "options": [
                    {
                        "value_ref": "sum_of_complete_days",
                        "typed_value": {
                            "aggregation": "sum_of_complete_days",
                            "value_ref": "mean_of_complete_days",
                        },
                        "label": "比较阶段总额（推荐）",
                        "description": "回答各阶段贡献规模。",
                        "recommended": True,
                    },
                    {
                        "value_ref": "mean_of_complete_days",
                        "typed_value": {
                            "aggregation": "mean_of_complete_days",
                        },
                        "label": "比较日均金额",
                        "description": "控制不同阶段天数差异。",
                        "recommended": False,
                    },
                ],
                "recommendation_reason": "阶段总额直接回答贡献规模。",
            }
        ],
        "status_message": "等待确认。",
    }

    with pytest.raises(
        LLMOutputError,
        match=(
            "single_authority_clarification_projection_conflict:"
            r"questions\[0\]\.options\[0\]\.typed_value\.value_ref"
        ),
    ):
        langgraph_workflow._project_single_authority_clarification_output(
            output,
            slot_contracts=(contract,),
            time_spec=TARGET_RANGE,
        )


def test_clarification_projection_discards_one_invalid_nonrecommended_typed_option() -> (
    None
):
    slot = _comparison_interpretation_slot()
    contract = langgraph_workflow._single_authority_clarification_slot_contract(
        slot=slot,
        comparison_spec={
            "kind": "decision_slot",
            "slot_id": "comparison_interpretation",
        },
    )
    definitions = [
        {"member": "start", "day_start": 1, "day_end": 3},
        {"member": "mid", "day_start": 4, "day_end": 27},
        {"member": "end", "day_start": 28, "day_end": 31},
    ]
    output = {
        "questions": [
            {
                "slot_id": "comparison_interpretation",
                "question": "请选择逐月比较方式。",
                "options": [
                    {
                        "value_ref": "interpretation_1",
                        "typed_value": {
                            "kind": "calendar_partition",
                            "baseline_class": "prior_period",
                            "period_grain": "month",
                            "partition_field": "month_phase",
                            "target_members": ["start"],
                            "baseline_members": ["end"],
                            "aggregation": "sum_of_complete_days",
                            "member_definitions": definitions,
                        },
                        "label": "月初与上月末逐月比较（推荐）",
                        "description": "按每个月进行动态配对。",
                        "recommended": True,
                    },
                    {
                        "value_ref": "interpretation_2",
                        "typed_value": {
                            "kind": "fixed_window",
                            "baseline_class": "value_ref",
                            "baseline_start": "2026-04-28",
                            "baseline_end": "2026-04-30",
                            "aggregation": "sum_of_complete_days",
                        },
                        "label": "固定窗口比较",
                        "description": "只使用一个固定基线窗口。",
                        "recommended": False,
                    },
                    {
                        "value_ref": "interpretation_3",
                        "typed_value": {
                            "kind": "calendar_partition",
                            "baseline_class": "same_month_phase",
                            "period_grain": "month",
                            "partition_field": "month_phase",
                            "target_members": ["start"],
                            "baseline_members": ["end"],
                            "aggregation": "sum_of_complete_days",
                            "member_definitions": definitions,
                        },
                        "label": "月初与当月末比较",
                        "description": "在同一个月内比较两个阶段。",
                        "recommended": False,
                    },
                ],
                "recommendation_reason": "逐月配对符合本次分析范围。",
            }
        ],
        "status_message": "等待确认。",
    }

    projection = langgraph_workflow._project_single_authority_clarification_output(
        output,
        slot_contracts=(contract,),
        time_spec=TARGET_RANGE,
    )
    projected = dict(projection.output)

    assert projection.disposition == "accepted_normalized"
    assert [option["value_ref"] for option in projected["questions"][0]["options"]] == [
        "interpretation_1",
        "interpretation_3",
    ]
    assert dict(projection.mutations[-1]) == {
        "path": "questions[0].options[1]",
        "action": "discard_invalid_option",
        "reason": "typed_value_outside_consumer_contract",
    }
    questions = (
        langgraph_workflow._validate_single_authority_clarification_batch_output(
            projected,
            slot_contracts=(contract,),
            time_spec=TARGET_RANGE,
        )
    )
    assert len(questions[0]["options"]) == 2


def test_clarification_projection_can_retain_only_the_valid_recommended_option() -> (
    None
):
    slot = _comparison_interpretation_slot()
    contract = langgraph_workflow._single_authority_clarification_slot_contract(
        slot=slot,
        comparison_spec={
            "kind": "decision_slot",
            "slot_id": "comparison_interpretation",
        },
    )
    definitions = [
        {"member": "start", "day_start": 1, "day_end": 10},
        {"member": "mid", "day_start": 11, "day_end": 20},
        {"member": "end", "day_start": 21, "day_end": 31},
    ]
    options = [
        {
            "value_ref": "interpretation_1",
            "typed_value": {
                "kind": "calendar_partition",
                "baseline_class": "prior_period",
                "period_grain": "month",
                "partition_field": "month_phase",
                "target_members": ["start"],
                "baseline_members": ["end"],
                "aggregation": "sum_of_complete_days",
                "member_definitions": definitions,
            },
            "label": "月初与上月末比较（推荐）",
            "description": "逐月比较月初和上月末。",
            "recommended": True,
        },
    ]
    for index, partition_field in enumerate(
        ("month_of_year", "quarter_of_year"),
        start=2,
    ):
        member = 3 if partition_field == "month_of_year" else "Q2"
        options.append(
            {
                "value_ref": f"interpretation_{index}",
                "typed_value": {
                    "kind": "calendar_partition",
                    "baseline_class": "prior_period",
                    "period_grain": "year",
                    "partition_field": partition_field,
                    "target_members": [member],
                    "baseline_members": [member],
                    "aggregation": "sum_of_complete_days",
                },
                "label": f"无效备选{index}",
                "description": "目标与基线成员发生重叠。",
                "recommended": False,
            }
        )
    output = {
        "questions": [
            {
                "slot_id": "comparison_interpretation",
                "question": "请选择比较方式。",
                "options": options,
                "recommendation_reason": "推荐项匹配业务问题。",
            }
        ],
        "status_message": "等待确认。",
    }

    projection = langgraph_workflow._project_single_authority_clarification_output(
        output,
        slot_contracts=(contract,),
        time_spec=TARGET_RANGE,
    )
    projected = dict(projection.output)

    assert [option["value_ref"] for option in projected["questions"][0]["options"]] == [
        "interpretation_1"
    ]
    assert (
        len(
            langgraph_workflow._validate_single_authority_clarification_batch_output(
                projected,
                slot_contracts=(contract,),
                time_spec=TARGET_RANGE,
            )[0]["options"]
        )
        == 1
    )


@pytest.mark.parametrize(
    "compact_definitions",
    [
        {
            "start": [1, 1],
            "mid": [2, 30],
            "end": [31, 31],
        },
        [[1, 1], [2, 30], [31, 31]],
    ],
)
def test_clarification_projection_normalizes_compact_month_phase_ranges(
    compact_definitions: object,
) -> None:
    slot = _comparison_interpretation_slot()
    contract = langgraph_workflow._single_authority_clarification_slot_contract(
        slot=slot,
        comparison_spec={
            "kind": "decision_slot",
            "slot_id": "comparison_interpretation",
        },
    )
    options = []
    ranges = (
        (1, 1, 2, 30, 31, 31),
        (1, 3, 4, 28, 29, 31),
    )
    for index, raw_range in enumerate(ranges, start=1):
        definitions = (
            compact_definitions
            if index == 1
            else [
                [raw_range[0], raw_range[1]],
                [raw_range[2], raw_range[3]],
                [raw_range[4], raw_range[5]],
            ]
        )
        options.append(
            {
                "value_ref": f"interpretation_{index}",
                "typed_value": {
                    "kind": "calendar_partition",
                    "baseline_class": "prior_period",
                    "period_grain": "month",
                    "partition_field": "month_phase",
                    "target_members": ["start"],
                    "baseline_members": ["end"],
                    "aggregation": (
                        "sum_of_complete_days"
                        if index == 1
                        else "mean_of_complete_days"
                    ),
                    "member_definitions": definitions,
                },
                "label": (
                    "单日比较（推荐）" if index == 1 else "前3日后3日均值"
                ),
                "description": "使用明确的月初、月中和月末范围。",
                "recommended": index == 1,
            }
        )
    output = {
        "questions": [
            {
                "slot_id": "comparison_interpretation",
                "question": "请选择月初和月末范围。",
                "options": options,
                "recommendation_reason": "单日口径边界清晰。",
            }
        ],
        "status_message": "等待确认。",
    }

    projection = langgraph_workflow._project_single_authority_clarification_output(
        output,
        slot_contracts=(contract,),
        time_spec=TARGET_RANGE,
    )
    projected = dict(projection.output)

    assert projected["questions"][0]["options"][0]["typed_value"][
        "member_definitions"
    ] == [
        {"member": "start", "day_start": 1, "day_end": 1},
        {"member": "mid", "day_start": 2, "day_end": 30},
        {"member": "end", "day_start": 31, "day_end": 31},
    ]
    assert any(
        mutation.get("action") == "normalize_typed_structure"
        for mutation in projection.mutations
    )
    questions = (
        langgraph_workflow._validate_single_authority_clarification_batch_output(
            projected,
            slot_contracts=(contract,),
            time_spec=TARGET_RANGE,
        )
    )
    assert len(questions[0]["options"]) == 2


def test_month_phase_clarification_records_machine_identifiers_without_blocking() -> None:
    slot = {
        "slot_id": "phase_aggregation",
        "slot_kind": "phase_aggregation",
        "materiality": "material",
        "status": "unresolved",
        "question": "比较阶段总额还是日均金额？",
        "allowed_value_refs": [
            "sum_of_complete_days",
            "mean_of_complete_days",
        ],
    }
    output = {
        "questions": [
            {
                "slot_id": "phase_aggregation",
                "question": (
                    "比较 sum_of_complete_days 还是 mean_of_complete_days？"
                ),
                "options": [
                    {
                        "value_ref": "sum_of_complete_days",
                        "typed_value": {
                            "aggregation": "sum_of_complete_days",
                        },
                        "label": "比较阶段总额（推荐）",
                        "description": "回答各阶段贡献了多少付费金额。",
                        "recommended": True,
                    },
                    {
                        "value_ref": "mean_of_complete_days",
                        "typed_value": {
                            "aggregation": "mean_of_complete_days",
                        },
                        "label": "比较日均金额",
                        "description": "控制不同阶段天数差异。",
                        "recommended": False,
                    },
                ],
                "recommendation_reason": "阶段总额直接回答贡献规模。",
            }
        ],
        "status_message": "等待确认。",
    }
    options = langgraph_workflow._validate_single_authority_clarification_output(
        {
            "question": output["questions"][0]["question"],
            "options": output["questions"][0]["options"],
            "recommendation_reason": output["questions"][0][
                "recommendation_reason"
            ],
            "status_message": output["status_message"],
        },
        slot=slot,
        time_spec={
            "kind": "date_range",
            "start": "2024-01-01",
            "end": "2026-05-31",
        },
        required_recommended_value_ref="sum_of_complete_days",
        required_recommended_label="",
        required_recommended_typed_value={
            "aggregation": "sum_of_complete_days",
        },
    )
    issues = langgraph_workflow._clarification_public_language_issues(
        output=output,
        slot_contracts=({"slot": slot},),
    )

    assert len(options) == 2
    assert issues == [
        {
            "issue_code": "machine_identifier_exposed",
            "slot_id": "phase_aggregation",
            "visible_fields": ["question"],
        }
    ]


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
        comparison_spec = {
            "kind": "calendar_partition",
            "baseline_class": baseline_class,
            "period_grain": period_grain,
            "partition_field": partition_field,
            "target_members": target_members,
            "baseline_members": baseline_members,
            "aggregation": "mean_of_complete_days",
        }
        if partition_field == "month_phase":
            comparison_spec["member_definitions"] = [
                {"member": "start", "day_start": 1, "day_end": 10},
                {"member": "mid", "day_start": 11, "day_end": 20},
                {"member": "end", "day_start": 21, "day_end": 31},
            ]
        validate_comparison_spec(
            comparison_spec,
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


def test_clarification_repair_reports_the_exact_typed_structure_error() -> None:
    time_spec = {
        "kind": "date_range",
        "start": "2024-01-01",
        "end": "2026-05-31",
    }
    typed_value = {
        "kind": "calendar_partition",
        "baseline_class": "same_month_phase",
        "period_grain": "month",
        "partition_field": "month_phase",
        "target_members": ["start"],
        "baseline_members": ["end"],
        "aggregation": "sum_of_complete_days",
        "member_definitions": {
            "start": [1, 5],
            "mid": [6, 24],
            "end": [25, 31],
        },
    }
    output = {
        "question": "请选择月内阶段比较口径。",
        "options": [
            {
                "value_ref": "interpretation_1",
                "typed_value": typed_value,
                "label": "按三个阶段比较总额（推荐）",
                "description": "用于比较每月不同阶段的付费总额。",
                "recommended": True,
            },
            {
                "value_ref": "interpretation_2",
                "typed_value": {
                    **typed_value,
                    "aggregation": "mean_of_complete_days",
                },
                "label": "按三个阶段比较日均金额",
                "description": "用于控制阶段天数差异。",
                "recommended": False,
            },
        ],
        "recommendation_reason": "阶段总额与当前问题更一致。",
        "status_message": "等待确认。",
    }

    with pytest.raises(
        LLMOutputError,
        match=(
            "single_authority_clarification_typed_value_invalid:"
            "slot=comparison_interpretation,"
            "detail=member_definitions_list_required"
        ),
    ) as captured:
        langgraph_workflow._validate_single_authority_clarification_output(
            output,
            slot=_comparison_interpretation_slot(),
            time_spec=time_spec,
            required_recommended_value_ref="",
            required_recommended_label="",
        )
    assert captured.value.repair_contract == {
        "typed_value": {
            "type": "object",
            "exact_fields": [
                "kind",
                "baseline_class",
                "period_grain",
                "partition_field",
                "target_members",
                "baseline_members",
                "aggregation",
                "member_definitions",
            ],
            "field_contracts": {
                "kind": "calendar_partition",
                "target_members": "array",
                "baseline_members": "array",
                "member_definitions": {
                    "type": "array",
                    "length": 3,
                    "items_in_order": [
                        {
                            "member": "start",
                            "day_start": "integer",
                            "day_end": "integer",
                        },
                        {
                            "member": "mid",
                            "day_start": "integer",
                            "day_end": "integer",
                        },
                        {
                            "member": "end",
                            "day_start": "integer",
                            "day_end": "integer",
                        },
                    ],
                    "item_exact_fields": ["member", "day_start", "day_end"],
                    "constraints": [
                        "ranges_are_contiguous",
                        "ranges_cover_days_1_through_31",
                    ],
                },
            },
        }
    }


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
        match=(
            "intent_revision_comparison_authority_invalid:"
            "temporal_comparison_authority_conflict"
        ),
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


def test_main_comparison_slot_can_keep_atomic_month_phase_parameter_slots() -> None:
    catalog = {
        str(slot["slot_id"]): slot
        for slot in langgraph_workflow._single_authority_ambiguity_slot_catalog()
    }
    override_slots = tuple(
        {
            "slot_id": slot_id,
            "slot_kind": catalog[slot_id]["slot_kind"],
            "materiality": "material",
            "status": "unresolved",
            "question": "请确认这个独立的比较口径。",
            "allowed_value_refs": catalog[slot_id]["allowed_value_refs"],
        }
        for slot_id in ("month_phase_definition", "phase_aggregation")
    )
    intent = _intent(
        time_spec=TARGET_RANGE,
        slot_id="comparison_interpretation",
        extra_slots=override_slots,
    )

    main_decision = DecisionRecord.create(
        intent_revision_id=intent.intent_revision_id,
        slot_id="comparison_interpretation",
        value={
            "kind": "calendar_partition",
            "baseline_class": "prior_period",
            "period_grain": "month",
            "partition_field": "month_phase",
            "target_members": ["start"],
            "baseline_members": ["end"],
            "aggregation": "mean_of_complete_days",
            "member_definitions": [
                {"member": "start", "day_start": 1, "day_end": 10},
                {"member": "mid", "day_start": 11, "day_end": 20},
                {"member": "end", "day_start": 21, "day_end": 31},
            ],
        },
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=("baseline_refs", "resolved_window_refs"),
        option_id="comparison_interpretation.interpretation_test",
    )
    boundary_decision = DecisionRecord.create(
        intent_revision_id=intent.intent_revision_id,
        slot_id="month_phase_definition",
        value={
            "value_ref": "definition_1",
            "member_definitions": [
                {"member": "start", "day_start": 1, "day_end": 5},
                {"member": "mid", "day_start": 6, "day_end": 24},
                {"member": "end", "day_start": 25, "day_end": 31},
            ],
        },
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=("comparison_spec.member_definitions",),
        option_id="month_phase_definition.definition_1",
    )
    aggregation_decision = DecisionRecord.create(
        intent_revision_id=intent.intent_revision_id,
        slot_id="phase_aggregation",
        value={"aggregation": "sum_of_complete_days"},
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=("comparison_spec.aggregation",),
        option_id="phase_aggregation.sum_of_complete_days",
    )
    ledger = (
        DecisionLedger()
        .append(main_decision)
        .append(boundary_decision)
        .append(aggregation_decision)
    )

    authority = resolve_effective_comparison(
        time_spec=intent.time_spec,
        comparison_spec=intent.comparison_spec,
        decision_ledger=ledger,
        require_physical_baseline=False,
    )

    assert authority.mode == "calendar_partition"
    assert authority.calendar_partition["member_definitions"] == (
        {"member": "start", "day_start": 1, "day_end": 5},
        {"member": "mid", "day_start": 6, "day_end": 24},
        {"member": "end", "day_start": 25, "day_end": 31},
    )
    assert authority.calendar_partition["aggregation"] == "sum_of_complete_days"
    assert set(authority.decision_refs) == {
        main_decision.decision_id,
        boundary_decision.decision_id,
        aggregation_decision.decision_id,
    }
    planner_temporal = (
        langgraph_workflow._planner_effective_temporal_comparison(
            intent_revision=intent,
            decision_ledger=ledger,
        )
    )
    assert planner_temporal["intent_comparison_spec"] == {
        "kind": "decision_slot",
        "slot_id": "comparison_interpretation",
    }
    assert planner_temporal["effective_comparison_spec"]["aggregation"] == (
        "sum_of_complete_days"
    )
    assert planner_temporal["effective_comparison_spec"]["member_definitions"] == [
        {"member": "start", "day_start": 1, "day_end": 5},
        {"member": "mid", "day_start": 6, "day_end": 24},
        {"member": "end", "day_start": 25, "day_end": 31},
    ]


def test_unresolved_main_comparison_does_not_invent_field_recommendation_contracts() -> (
    None
):
    catalog = {
        str(slot["slot_id"]): slot
        for slot in langgraph_workflow._single_authority_ambiguity_slot_catalog()
    }
    comparison_spec = {
        "kind": "decision_slot",
        "slot_id": "comparison_interpretation",
    }

    boundary_contract = (
        langgraph_workflow._single_authority_clarification_slot_contract(
            slot=catalog["month_phase_definition"],
            comparison_spec=comparison_spec,
        )
    )
    aggregation_contract = (
        langgraph_workflow._single_authority_clarification_slot_contract(
            slot=catalog["phase_aggregation"],
            comparison_spec=comparison_spec,
        )
    )

    assert boundary_contract["recommended_value_ref"] == ""
    assert boundary_contract["required_recommended_typed_value"] is None
    assert aggregation_contract["recommended_value_ref"] == ""
    assert aggregation_contract["required_recommended_typed_value"] is None


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

    assert prompt.prompt_version.endswith(".v29")
    assert prompt.required_keys == (
        "intent_binding",
        "comparison_grounding",
        "business_summary",
    )
    assert "return exactly one scalar value" in prompt_text
    assert "requested_factor_refs" in prompt_text
    assert "quarter-to-quarter" in prompt_text
    assert "comparison_baseline for kind date" in prompt_text
    assert "comparison_window for kind date_range" in prompt_text
    assert (
        "Do not choose between those slots from business-question keywords"
        in prompt_text
    )
    assert "clarification remains an LLM semantic judgment" in prompt_text
    assert "Never rely on a default implication" in prompt_text
    assert "use the field-level slots described above" in prompt_text
    assert "month_phase_definition" in prompt_text
    assert "phase_aggregation" in prompt_text
    assert "calendar_partition_contracts" in prompt_text
    assert "Explicit anomaly or outlier inspection" in prompt_text
    assert "member_definitions" in prompt_text
    assert "never name a decision slot that is absent" in prompt_text
    assert "business_summary is the accepted user-facing projection" in prompt_text
    assert "comparison_grounding as an independent extraction" in prompt_text
    assert "never return kind none for that directional premise" in prompt_text
    assert "Keep that question fully customer-facing" in prompt_text
    assert "typed_value must be one complete fixed_window or" in clarification_text
    assert "runtime supplies a separate free-text outlet" in clarification_text
    assert "must use a distinct set of day boundaries" in clarification_text
    assert "not approved display copy" in clarification_text
    assert "absent from the supplied comparison_spec" in clarification_text
    assert _comparison_window_slot()["time_spec_kinds"] == ["date_range"]
    payload = langgraph_workflow._single_authority_intent_payload(
        question="比较月初、月中和月末付费金额。",
        registry=RuntimeContractRegistry.from_path(
            CANONICAL_RUNTIME_BINDINGS_PATH
        ),
    )
    month_phase = payload["comparison_spec_contract"][
        "calendar_partition_contracts"
    ]["month_phase"]
    assert month_phase == {
        "period_grain": "month",
        "baseline_classes": ["prior_period", "same_month_phase"],
        "members": ["start", "mid", "end"],
    }
    assert payload["comparison_spec_contract"]["variants"]["decision_slot"][
        "slot_id"
    ] == [
        "comparison_baseline",
        "comparison_interpretation",
        "comparison_window",
        "event_relative_window",
    ]


def test_successor_intent_receives_the_source_intent_as_revision_context() -> None:
    source = _intent(
        time_spec=TARGET_RANGE,
        slot_id="comparison_interpretation",
    )
    payload = langgraph_workflow._single_authority_intent_payload(
        question="月初1—5日、月中6—24日、月末25—31日，比较阶段总额。",
        registry=RuntimeContractRegistry.from_path(
            CANONICAL_RUNTIME_BINDINGS_PATH
        ),
        source_intent_revision=source,
        superseded_plan_fields=("baseline_refs", "resolved_window_refs"),
    )

    revision_context = payload["revision_context"]
    assert revision_context["source_original_user_text"] == (
        source.original_user_text
    )
    assert revision_context["source_intent_binding"]["goal_bindings"] == [
        {"goal_id": "explain_change", "role": "primary"}
    ]
    assert revision_context["source_intent_binding"]["comparison_spec"] == {
        "kind": "decision_slot",
        "slot_id": "comparison_interpretation",
    }
    assert revision_context["superseded_plan_fields"] == [
        "baseline_refs",
        "resolved_window_refs",
    ]
    prompt = build_prompt("single_authority_intent", payload)
    prompt_text = prompt.messages[-1]["content"]
    assert "preserve every unchanged business judgment" in prompt_text
    assert "Do not broaden a named target-versus-baseline judgment" in prompt_text


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
