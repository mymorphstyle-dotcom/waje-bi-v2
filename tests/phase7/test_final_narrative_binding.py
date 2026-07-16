from __future__ import annotations

from bi_agent.runtime.final_narrative_binding import (
    build_final_narrative_publication_binding,
    build_narrative_authority_record,
    build_narrative_question_scope,
    final_narrative_binding_errors,
)
from bi_agent.runtime.llm_prompts import build_prompt


def _comparison_claim(*, target: str = "100", baseline: str = "90") -> dict:
    return {
        "claim_ref": "claim:paid-amount-change",
        "claim_type": "comparative_change",
        "claim_strength": "observed",
        "text": (
            "2026-06-01的付费金额为100，"
            "2026-05-31的付费金额为90，增加10，变化11.11%。"
        ),
        "target_metric": "paid_amount",
        "target": {"label": "2026-06-01", "role": "target"},
        "baseline": {"label": "2026-05-31", "role": "baseline"},
        "numbers": {
            "target_value": target,
            "baseline_value": baseline,
            "absolute_change": "10",
            "relative_change": "0.1111111111",
        },
        "evidence_refs": ["evidence:paid-amount-change"],
    }


def _authority_record(claim: dict | None = None) -> dict:
    claim = claim or _comparison_claim()
    return build_narrative_authority_record(
        verified_claims=(claim,),
        evidence=(
            {
                "evidence_ref": "evidence:paid-amount-change",
                "evidence_type": "statistical_association",
                "binding_manifest_digest": "sha256:evidence",
                "typed_payload": {},
            },
        ),
        visible_limitations=(),
        accepted_assumptions=(),
    )


def _binding(excerpt: str, *, statement_class: str = "verified_claim") -> tuple[dict, ...]:
    return (
        {
            "excerpt": excerpt,
            "statement_class": statement_class,
            "authority_keys": ["结论1"],
        },
    )


def _quality_gate() -> dict:
    return {
        "status": "passed",
        "display_status": "ready",
        "blocks_display": False,
        "hard_blockers": [],
        "repairable_warnings": [],
        "risk_flags": [],
    }


def _multi_claim_authority() -> dict:
    target = {"label": "2026-06-01", "role": "target"}
    baseline = {"label": "2026-05-31", "role": "baseline"}
    formula_claim = {
        "claim_ref": "claim:formula-contribution",
        "claim_type": "formula_component_contribution",
        "claim_strength": "high",
        "text": (
            "单笔付费金额贡献126.2%，付费人数贡献2.0%，"
            "付费频次贡献-28.2%。"
        ),
        "target_metric": "paid_amount",
        "target": target,
        "baseline": baseline,
        "numbers": {
            "avg_order_amount_contribution": "5172408.34",
            "avg_order_amount_contribution_share": "1.262",
            "paid_users_contribution": "81091.78",
            "paid_users_contribution_share": "0.020",
            "paid_frequency_contribution": "-1155821.12",
            "paid_frequency_contribution_share": "-0.282",
        },
        "evidence_refs": ["evidence:formula-contribution"],
    }
    comparison_claim = {
        **_comparison_claim(target="308240309", baseline="304142630"),
        "text": (
            "2026-06-01的付费金额为308240309，"
            "2026-05-31的付费金额为304142630，"
            "增加4097679，变化1.35%。"
        ),
        "numbers": {
            "target_value": "308240309",
            "baseline_value": "304142630",
            "absolute_change": "4097679",
            "relative_change": "0.0135",
        },
        "comparison_direction": "positive",
    }
    segment_claim = {
        "claim_ref": "claim:segment-observation",
        "claim_type": "segment_contribution_or_mix_shift",
        "claim_strength": "medium",
        "text": (
            "拉各斯州目标期付费金额135701843，"
            "基线期128826283，增加6875560。"
        ),
        "target_metric": "paid_amount",
        "target": target,
        "baseline": baseline,
        "dimensions": {"region": "拉各斯州"},
        "numbers": {
            "paid_amount_target_value": "135701843",
            "paid_amount_baseline_value": "128826283",
            "paid_amount_delta": "6875560",
        },
        "evidence_refs": ["evidence:segment-observation"],
    }
    return build_narrative_authority_record(
        verified_claims=(formula_claim, comparison_claim, segment_claim),
        evidence=(
            {
                "evidence_ref": "evidence:formula-contribution",
                "evidence_type": "accounting_contribution",
                "binding_manifest_digest": "sha256:formula",
                "typed_payload": {
                    "decompositions": [
                        {
                            "component_changes": [
                                {
                                    "component_id": "paid_users",
                                    "business_name": "付费人数",
                                    "observed": True,
                                    "baseline_value": "37754",
                                    "target_value": "37764",
                                    "delta": "10",
                                    "delta_ratio": "0.00026",
                                },
                                {
                                    "component_id": "paid_frequency",
                                    "business_name": "付费频次",
                                    "observed": True,
                                    "baseline_value": "3.878",
                                    "target_value": "3.863",
                                    "delta": "-0.015",
                                    "delta_ratio": "-0.0038",
                                },
                                {
                                    "component_id": "avg_order_amount",
                                    "business_name": "单笔付费金额",
                                    "observed": True,
                                    "baseline_value": "2077.29",
                                    "target_value": "2112.68",
                                    "delta": "35.39",
                                    "delta_ratio": "0.017",
                                },
                                {
                                    "component_id": "first_paid_users",
                                    "business_name": "首充人数",
                                    "observed": True,
                                    "baseline_value": "6075",
                                    "target_value": "5711",
                                    "delta": "-364",
                                    "delta_ratio": "-0.06",
                                },
                                {
                                    "component_id": "payment_success_rate",
                                    "business_name": "支付成功率",
                                    "observed": False,
                                },
                            ],
                            "core_factor_contributions": [
                                {
                                    "component_id": "paid_users",
                                    "contribution": "81091.78",
                                    "contribution_share": "0.020",
                                },
                                {
                                    "component_id": "paid_frequency",
                                    "contribution": "-1155821.12",
                                    "contribution_share": "-0.282",
                                },
                                {
                                    "component_id": "avg_order_amount",
                                    "contribution": "5172408.34",
                                    "contribution_share": "1.262",
                                },
                            ],
                        }
                    ]
                },
            },
            {
                "evidence_ref": "evidence:paid-amount-change",
                "evidence_type": "statistical_association",
                "binding_manifest_digest": "sha256:comparison",
                "typed_payload": {},
            },
            {
                "evidence_ref": "evidence:segment-observation",
                "evidence_type": "statistical_association",
                "binding_manifest_digest": "sha256:segment",
                "typed_payload": {},
            },
        ),
        visible_limitations=(),
        accepted_assumptions=(),
    )


def _statement(
    excerpt: str,
    statement_class: str,
    *authority_keys: str,
) -> dict:
    return {
        "excerpt": excerpt,
        "statement_class": statement_class,
        "authority_keys": list(authority_keys),
    }


def test_target_and_baseline_values_cannot_be_swapped() -> None:
    narrative = "目标期付费金额为90，基线期为100。"
    record = _authority_record()

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=_binding(narrative),
        authority_record=record,
        quality_gate=_quality_gate(),
    )

    assert binding["status"] == "rejected"
    assert "authority_role_mismatch" in errors


def test_boundary_prefix_cannot_hide_positive_causal_claim() -> None:
    narrative = "目标期付费金额为100。无法确认促销细节，但促销活动导致付费上涨。"
    record = _authority_record()

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=_binding(narrative),
        authority_record=record,
        quality_gate=_quality_gate(),
    )

    assert binding["status"] == "rejected"
    assert "unsupported_causal_statement" in errors


def test_delivery_rebuilds_authority_record_instead_of_trusting_self_reported_digest() -> None:
    narrative = "2026年6月1日付费金额为100，较2026年5月31日的90增加10。"
    record = _authority_record()
    statement_bindings = _binding(narrative)
    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=statement_bindings,
        authority_record=record,
        quality_gate=_quality_gate(),
    )
    assert errors == ()

    changed_record = _authority_record(
        _comparison_claim(target="101", baseline="90")
    )
    delivery_errors = final_narrative_binding_errors(
        binding=binding,
        narrative=narrative,
        statement_bindings=statement_bindings,
        authority_record=changed_record,
        quality_gate=_quality_gate(),
    )

    assert "authority_record_mismatch" in delivery_errors


def test_equivalent_chinese_dates_and_rounded_wan_yi_formats_are_publishable() -> None:
    claim = {
        **_comparison_claim(target="308240309", baseline="304142630"),
        "text": (
            "2026-06-01的付费金额为308240309，"
            "2026-05-31的付费金额为304142630，增加4097679。"
        ),
        "numbers": {
            "target_value": "308240309",
            "baseline_value": "304142630",
            "absolute_change": "4097679",
            "relative_change": "0.013472886",
        },
    }
    narrative = "2026年6月1日付费金额约3.08亿，较2026年5月31日增加约410万。"

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=_binding(narrative),
        authority_record=_authority_record(claim),
        quality_gate=_quality_gate(),
    )

    assert errors == ()
    assert binding["status"] == "bound"


def test_material_sentence_binding_does_not_need_section_heading() -> None:
    finding = (
        "目标日付费金额为308,240,309，较基线日304,142,630"
        "上涨1.35%，增加4,097,679"
    )
    conclusion = "目标日付费金额较基线日确认上涨1.35%"
    narrative = "\n\n".join(
        (
            "我对问题的理解是：核对目标日与基线日的付费金额变化。",
            "分析脉络：先核对方向，再拆解因素。",
            f"关键发现：{finding}。",
            f"最终结论：{conclusion}。",
            "需要注意：后续检查只作为建议。",
        )
    )

    _, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(finding, "verified_claim", "结论2"),
            _statement(conclusion, "verified_claim", "结论2"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert errors == ()


def test_dimension_role_context_survives_commas_inside_bound_statement() -> None:
    finding = (
        "地区维度中，拉各斯州变化最明显（目标期135,701,843，"
        "基线期128,826,283，增加6,875,560）"
    )
    narrative = f"关键发现：{finding}。"

    _, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(finding, "verified_claim", "结论3"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert errors == ()


def test_numbered_next_checks_are_not_business_measurements() -> None:
    finding = "目标日付费金额较基线日上涨1.35%"
    next_check = (
        "可继续核查：1. 查看主要地区；2. 检查付费频次；"
        "3. 确认支付成功率是否变化"
    )
    narrative = f"关键发现：{finding}。\n需要注意：{next_check}。"

    _, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(finding, "verified_claim", "结论2"),
            _statement(next_check, "next_check", "原因边界"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert errors == ()


def test_compact_bindings_can_jointly_cover_one_material_sentence() -> None:
    avg_order = "单笔付费金额贡献126.2%"
    paid_users = "付费人数贡献2.0%"
    frequency = "付费频次贡献-28.2%"
    narrative = f"关键发现：三因素拆解显示，{avg_order}，{paid_users}，{frequency}。"

    _, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(avg_order, "factor_contribution", "单笔付费金额"),
            _statement(paid_users, "factor_contribution", "付费人数"),
            _statement(frequency, "factor_contribution", "付费频次"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert errors == ()


def test_problem_scope_carries_explicit_target_and_baseline_dates() -> None:
    formula_only = build_narrative_authority_record(
        verified_claims=(
            {
                "claim_ref": "claim:formula-only",
                "claim_type": "formula_component_contribution",
                "claim_strength": "high",
                "text": "单笔付费金额是主要贡献项。",
                "target_metric": "paid_amount",
                "target": {"label": "2026-06-01", "role": "target"},
                "baseline": {},
                "numbers": {},
                "evidence_refs": ["evidence:formula-only"],
            },
        ),
        evidence=(
            {
                "evidence_ref": "evidence:formula-only",
                "evidence_type": "accounting_contribution",
                "binding_manifest_digest": "sha256:formula-only",
                "typed_payload": {},
            },
        ),
        visible_limitations=(),
        accepted_assumptions=(),
        question_scope=build_narrative_question_scope(
            {
                "scope": {"type": "full_sample"},
                "resolved_windows": [
                    {
                        "window_id": "target_day",
                        "role": "target",
                        "label": "2026-06-01",
                    },
                    {
                        "window_id": "previous_day",
                        "role": "baseline",
                        "label": "2026-05-31",
                    },
                ],
            }
        ),
    )
    scope = "分析基于全样本，目标窗口为2026-06-01，基线为2026-05-31"

    _, errors = build_final_narrative_publication_binding(
        narrative=scope,
        statement_bindings=(
            _statement(scope, "analysis_scope", "问题范围"),
        ),
        authority_record=formula_only,
        quality_gate=_quality_gate(),
    )

    assert errors == ()


def test_formula_contribution_allows_accounting_driver_and_offset_language() -> None:
    finding = (
        "单笔付费金额贡献126.2%，付费频次贡献-28.2%，"
        "付费人数贡献2.0%"
    )
    conclusion = (
        "付费金额上涨主要由单笔付费金额提升驱动，"
        "付费频次下降形成部分抵消，付费人数贡献较小"
    )
    narrative = f"关键发现：{finding}。\n最终结论：{conclusion}。"

    _, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(finding, "factor_contribution", "结论1"),
            _statement(conclusion, "verified_claim", "结论1"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert errors == ()


def test_negative_mechanism_boundary_is_publishable() -> None:
    boundary = "当前核算不说明更深层业务机制"
    narrative = f"最终结论：{boundary}。"

    _, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(boundary, "data_boundary", "原因边界"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert errors == ()


def test_unbound_material_fact_is_rejected() -> None:
    supported = "目标日付费金额较基线日上涨1.35%"
    narrative = f"关键发现：{supported}。另一个地区上涨50%。"

    _, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(supported, "verified_claim", "结论2"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert "material_statement_unbound" in errors


def test_valid_values_with_reversed_direction_are_rejected() -> None:
    narrative = (
        "目标日付费金额为308,240,309，较基线日304,142,630"
        "下跌1.35%，减少4,097,679。"
    )

    _, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(narrative, "verified_claim", "结论2"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert "comparison_direction_mismatch" in errors


def test_narrative_cannot_upgrade_claim_strength() -> None:
    narrative = "地区维度中拉各斯州变化突出，属于高强度证据。"

    _, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(narrative, "verified_claim", "结论3"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert "claim_strength_escalation" in errors


def test_final_summary_prompt_makes_business_authority_and_compact_binding_clear() -> None:
    prompt = build_prompt(
        "final_business_summary",
        {
            "draftAnswer": "草稿",
            "businessContext": {},
            "displayReview": {},
        },
    )
    text = "\n".join(message["content"] for message in prompt.messages)

    assert prompt.required_keys == (
        "summary_text",
        "statement_bindings",
        "display_summary",
    )
    assert "draftAnswer is wording reference only" in text
    assert "businessContext.question, businessContext.evidence, and causalBoundary" in text
    assert "omit a draft statement when no matching authority is supplied" in text
    assert "Multiple compact excerpts may jointly cover one sentence" in text
    assert "List ordinals are formatting, not business measurements" in text
