from __future__ import annotations

from bi_agent.runtime.final_narrative_binding import (
    build_authority_safe_narrative,
    build_final_narrative_publication_binding,
    build_narrative_authority_record,
    build_narrative_question_scope,
    final_narrative_binding_errors,
)
from bi_agent.runtime.answer_package import (
    _cross_source_association_authority_fact,
    _project_derived_association_claim,
    _retain_publishable_bound_narrative,
    _requires_authority,
)


def test_authority_safe_narrative_reuses_verified_statements_and_rebinds() -> None:
    record = _authority_record()

    projection = build_authority_safe_narrative(record)
    binding, errors = build_final_narrative_publication_binding(
        narrative=projection["narrative"],
        statement_bindings=projection["statement_bindings"],
        authority_record=record,
        quality_gate={},
    )

    assert projection["status"] == "bound"
    assert record["claims"][0]["statement"] in projection["narrative"]
    assert projection["accepted_authority_keys"] == ["结论1", "原因边界"]
    assert errors == ()
    assert binding["status"] == "bound"


def test_cross_source_derived_finding_receives_bounded_publication_authority() -> None:
    evidence_ref = "evidence:cross-source-association"
    statement = (
        "付费金额与玩家投注金额的日变化呈稳定正向关联；"
        "该结果只作为跨数据源关联线索。"
    )
    record = build_narrative_authority_record(
        verified_claims=(_comparison_claim(),),
        evidence=(
            {
                "evidence_ref": "evidence:paid-amount-change",
                "evidence_type": "statistical_association",
                "typed_payload": {},
            },
            {
                "evidence_ref": evidence_ref,
                "evidence_type": "statistical_association",
                "result_refs": ["result:owned"],
                "typed_payload": {},
            },
        ),
        visible_limitations=(),
        accepted_assumptions=(),
        diagnostic_insights={
            "cross_source_findings": [
                {
                    "finding_type": "cross_source_temporal_association",
                    "evidence_state": "derived",
                    "statement": statement,
                    "coefficient": 0.42,
                    "source_evidence_refs": [evidence_ref],
                    "source_result_refs": ["result:owned"],
                }
            ]
        },
    )

    assert len(record["diagnostic_insights"]) == 1
    assert record["diagnostic_insights"][0]["statement"] == statement

    projection = build_authority_safe_narrative(record)

    assert statement in projection["narrative"]
    assert projection["status"] == "bound"


def test_cross_source_derived_finding_rejects_result_outside_source_envelope() -> None:
    record = build_narrative_authority_record(
        verified_claims=(_comparison_claim(),),
        evidence=(
            {
                "evidence_ref": "evidence:cross-source-association",
                "evidence_type": "statistical_association",
                "result_refs": ["result:owned"],
                "typed_payload": {},
            },
        ),
        visible_limitations=(),
        accepted_assumptions=(),
        diagnostic_insights={
            "cross_source_findings": [
                {
                    "finding_type": "cross_source_temporal_association",
                    "evidence_state": "derived",
                    "statement": "玩法指标与付费金额呈稳定统计关联。",
                    "source_evidence_refs": [
                        "evidence:cross-source-association"
                    ],
                    "source_result_refs": ["result:not-owned"],
                }
            ]
        },
    )

    assert record["diagnostic_insights"] == []


def test_partial_publication_removes_only_rejected_statement() -> None:
    claim = _comparison_claim()
    valid = claim["text"]
    retained = _retain_publishable_bound_narrative(
        narrative=f"{valid}\n未经验证的设备结论。",
        statement_bindings=(
            {
                "excerpt": valid,
                "statement_class": "verified_claim",
                "authority_keys": ["结论1"],
            },
            {
                "excerpt": "未经验证的设备结论。",
                "statement_class": "verified_claim",
                "authority_keys": ["结论2"],
            },
        ),
        published_claims=(claim,),
        evidence=(
            {
                "evidence_ref": "evidence:paid-amount-change",
                "evidence_type": "statistical_association",
                "typed_payload": {},
            },
        ),
        visible_limitations=(),
        accepted_assumptions=(),
        analysis_contract={},
        diagnostic_insights={},
        quality_gate={},
    )

    assert retained is not None
    assert retained["narrative"] == valid
    assert retained["statement_bindings"] == [
        {
            "excerpt": valid,
            "statement_class": "verified_claim",
            "authority_keys": ["结论1"],
        }
    ]
    assert retained["publication_binding"]["status"] == "bound"


def test_partial_publication_remaps_later_accepted_claim_keys() -> None:
    first = _comparison_claim(target="100", baseline="90")
    later = {
        **_comparison_claim(target="120", baseline="100"),
        "claim_ref": "claim:later-paid-amount-change",
        "text": (
            "2026-06-01的付费金额为120，"
            "2026-05-31的付费金额为100，增加20，变化20%。"
        ),
        "numbers": {
            "target_value": "120",
            "baseline_value": "100",
            "absolute_change": "20",
            "relative_change": "0.2",
        },
    }
    invalid = "未经验证的设备结论。"
    retained = _retain_publishable_bound_narrative(
        narrative=f"{first['text']}\n{invalid}\n{later['text']}",
        statement_bindings=(
            {
                "excerpt": first["text"],
                "statement_class": "verified_claim",
                "authority_keys": ["结论1"],
            },
            {
                "excerpt": invalid,
                "statement_class": "verified_claim",
                "authority_keys": ["结论2"],
            },
            {
                "excerpt": later["text"],
                "statement_class": "verified_claim",
                "authority_keys": ["结论3"],
            },
        ),
        published_claims=(first, later),
        evidence=(
            {
                "evidence_ref": "evidence:paid-amount-change",
                "evidence_type": "statistical_association",
                "typed_payload": {},
            },
        ),
        visible_limitations=(),
        accepted_assumptions=(),
        analysis_contract={},
        diagnostic_insights={},
        quality_gate={},
        source_claim_indexes=(0, 2),
    )

    assert retained is not None
    assert invalid not in retained["narrative"]
    assert first["text"] in retained["narrative"]
    assert later["text"] in retained["narrative"]
    assert retained["statement_bindings"][1]["authority_keys"] == ["结论2"]


def test_partial_publication_replaces_rejected_required_claim_with_canonical_fact() -> None:
    comparison = _comparison_claim()
    formula = {
        "claim_ref": "claim:formula-contribution",
        "claim_type": "formula_component_contribution",
        "claim_strength": "high",
        "text": "单笔付费金额是主要贡献项。",
        "target_metric": "paid_amount",
        "target": {"label": "2026-06-01", "role": "target"},
        "baseline": {"label": "2026-05-31", "role": "baseline"},
        "numbers": {"avg_order_amount_contribution_share": "1.262"},
        "evidence_refs": [],
    }
    unsupported_formula = "单笔付费金额贡献999%。"

    retained = _retain_publishable_bound_narrative(
        narrative=f"{comparison['text']}\n{unsupported_formula}",
        statement_bindings=(
            {
                "excerpt": comparison["text"],
                "statement_class": "verified_claim",
                "authority_keys": ["结论1"],
            },
            {
                "excerpt": unsupported_formula,
                "statement_class": "factor_contribution",
                "authority_keys": ["结论2"],
            },
        ),
        published_claims=(comparison, formula),
        evidence=(),
        visible_limitations=(),
        accepted_assumptions=(),
        analysis_contract={},
        diagnostic_insights={},
        quality_gate={},
        required_claim_types=(
            "comparative_change",
            "formula_component_contribution",
        ),
    )

    assert retained is not None
    assert unsupported_formula not in retained["narrative"]
    assert formula["text"] in retained["narrative"]
    assert comparison["text"] in retained["narrative"]


def test_partial_publication_rebinds_statement_to_unique_surviving_authority() -> None:
    first = _comparison_claim(target="100", baseline="90")
    later = {
        **_comparison_claim(target="120", baseline="100"),
        "claim_ref": "claim:later-paid-amount-change",
        "text": (
            "2026-06-01的付费金额为120，"
            "2026-05-31的付费金额为100，增加20，变化20%。"
        ),
        "numbers": {
            "target_value": "120",
            "baseline_value": "100",
            "absolute_change": "20",
            "relative_change": "0.2",
        },
    }

    retained = _retain_publishable_bound_narrative(
        narrative=later["text"],
        statement_bindings=(
            {
                "excerpt": later["text"],
                "statement_class": "verified_claim",
                "authority_keys": ["结论1"],
            },
        ),
        published_claims=(first, later),
        evidence=(),
        visible_limitations=(),
        accepted_assumptions=(),
        analysis_contract={},
        diagnostic_insights={},
        quality_gate={},
    )

    assert retained is not None
    assert retained["statement_bindings"][0]["authority_keys"] == ["结论2"]
    assert retained["authority_rebindings"] == [
        {
            "statement_index": 0,
            "submitted_authority_keys": ["结论1"],
            "rebound_authority_keys": ["结论2"],
        }
    ]


def test_stable_association_projects_as_derived_auxiliary_claim() -> None:
    payload = {
        "primary_outcome": "paid_amount",
        "scope": "full_sample",
        "time_window": "2026-06-01",
        "associations_by_outcome": {
            "paid_amount": {
                "association": {
                    "best_association": {
                        "candidate_key": "player_bet_amount",
                        "transform": "signed_log_difference",
                        "lag": 0,
                        "coefficient": 0.61,
                        "rolling": {"stable": True},
                    }
                }
            }
        },
    }
    evidence = {
        "evidence_ref": "association:stable",
        "capability_id": "cross_source_association",
        "strength": "medium",
        "wording_limit": "stable_association",
        "scope": "full_sample",
        "time_window": "2026-06-01",
        "typed_payload": payload,
    }

    fact = _cross_source_association_authority_fact(
        evidence,
        result_refs=("result:paid", "result:gameplay"),
    )
    projected = _project_derived_association_claim(
        {
            "claim_type": "cross_source_statistical_association",
            "claim_strength": "medium",
            "evidence_refs": ["association:stable"],
        },
        (fact,),
        {"grains": (("day",),)},
    )

    assert fact is not None
    assert projected["fact_refs"] == [fact["fact_ref"]]
    assert projected["numbers"] == {}
    assert "稳定正向统计关联" in projected["text"]
    assert "不能解释贡献金额或因果关系" in projected["text"]


def test_sensitivity_only_association_remains_diagnostic() -> None:
    evidence = {
        "evidence_ref": "association:sensitivity",
        "capability_id": "cross_source_panel_association",
        "strength": "low",
        "wording_limit": "sensitivity_only",
        "typed_payload": {},
    }

    assert _requires_authority(evidence) is False
    assert _cross_source_association_authority_fact(
        evidence,
        result_refs=("result:paid", "result:gameplay"),
    ) is None


def test_authority_safe_narrative_rejects_boundary_only_projection() -> None:
    record = build_narrative_authority_record(
        verified_claims=(),
        evidence=(),
        visible_limitations=("支付成功率当前没有独立观测。",),
        accepted_assumptions=(),
    )

    projection = build_authority_safe_narrative(record)

    assert projection["status"] == "rejected"
    assert projection["validation_errors"] == [
        "authority_safe_narrative_has_no_business_conclusion"
    ]


def test_authority_safe_narrative_omits_invalid_auxiliary_claim_only() -> None:
    primary = _comparison_claim()
    auxiliary = {
        **_comparison_claim(),
        "claim_ref": "claim:auxiliary-device",
        "claim_type": "business_object_candidate_impact",
        "text": "设备结构贡献了50%的付费金额上涨。",
        "numbers": {},
    }
    record = build_narrative_authority_record(
        verified_claims=(primary, auxiliary),
        evidence=(
            {
                "evidence_ref": "evidence:paid-amount-change",
                "evidence_type": "statistical_association",
                "typed_payload": {},
            },
        ),
        visible_limitations=(),
        accepted_assumptions=(),
    )

    projection = build_authority_safe_narrative(
        record,
        required_claim_types=("comparative_change",),
    )

    assert projection["status"] == "bound"
    assert "设备结构" not in projection["narrative"]
    assert projection["omitted_authorities"][0]["authority_key"] == "结论2"
    assert projection["missing_required_claim_types"] == []


def test_authority_safe_narrative_requires_each_required_claim_type() -> None:
    record = _authority_record()

    projection = build_authority_safe_narrative(
        record,
        required_claim_types=(
            "comparative_change",
            "formula_component_contribution",
        ),
    )

    assert projection["status"] == "rejected"
    assert projection["missing_required_claim_types"] == [
        "formula_component_contribution"
    ]


def test_authority_safe_narrative_keeps_uncovered_factor_numbers() -> None:
    record = _multi_claim_authority()
    formula_claim = next(
        claim
        for claim in record["claims"]
        if claim["claim_type"] == "formula_component_contribution"
    )
    formula_claim["statement"] = "单笔付费金额是主要贡献项。"

    projection = build_authority_safe_narrative(
        record,
        required_claim_types=("formula_component_contribution",),
    )

    assert projection["status"] == "bound"
    assert "单笔付费金额：已量化贡献" in projection["narrative"]
    assert "基线期2077.29" in projection["narrative"]
    assert "目标期2112.68" in projection["narrative"]
    assert "贡献份额126.20%" in projection["narrative"]


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


def _multi_claim_authority(
    *,
    diagnostic_insights: dict | None = None,
) -> dict:
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
        diagnostic_insights=diagnostic_insights,
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


def test_decimal_point_does_not_split_authorized_accounting_driver_clause() -> None:
    narrative = (
        "单笔付费金额贡献+5,172,408.34，贡献份额126.2%，"
        "为最主要驱动。"
    )

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(narrative, "factor_contribution", "结论1"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert errors == ()
    assert binding["status"] == "bound"


def test_unicode_minus_is_supported_for_negative_contribution_numbers() -> None:
    narrative = "付费频次贡献−1,155,821.12，贡献份额−28.2%。"

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(narrative, "factor_contribution", "结论1"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert errors == ()
    assert binding["status"] == "bound"


def test_growth_word_binds_absolute_change_role() -> None:
    narrative = "拉各斯州付费金额增长6,875,560。"

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(narrative, "factor_observation", "结论3"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert errors == ()
    assert binding["status"] == "bound"


def test_markdown_heading_and_line_break_are_not_unbound_material_facts() -> None:
    excerpt = "付费频次贡献−1,155,821.12，贡献份额−28.2%"
    narrative = f"### 贡献与抵消\n{excerpt}\n### 证据边界"

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(excerpt, "factor_contribution", "结论1"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert errors == ()
    assert binding["status"] == "bound"


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


def test_global_question_scope_dates_apply_to_each_supported_statement() -> None:
    record = build_narrative_authority_record(
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
                    {"role": "target", "label": "2026-06-01"},
                    {"role": "baseline", "label": "2026-05-31"},
                ],
            }
        ),
    )
    narrative = "结论仅适用于2026-06-01与2026-05-31的对比。"

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(narrative, "data_boundary", "结论1"),
        ),
        authority_record=record,
        quality_gate=_quality_gate(),
    )

    assert errors == ()
    assert binding["status"] == "bound"


def test_global_question_scope_keeps_target_and_baseline_date_roles_strict() -> None:
    narrative = "目标窗口为2026-05-31，基线窗口为2026-06-01。"

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(narrative, "analysis_scope", "问题范围"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert binding["status"] == "rejected"
    assert "authority_date_role_mismatch" in errors


def test_date_outside_global_question_scope_remains_rejected() -> None:
    narrative = "2026-06-02的付费金额上涨1.35%。"

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(narrative, "verified_claim", "结论2"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert binding["status"] == "rejected"
    assert "unsupported_narrative_date" in errors


def test_unknown_authority_key_remains_a_local_hard_rejection() -> None:
    narrative = "目标日付费金额较基线日上涨1.35%。"

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(narrative, "verified_claim", "不存在的结论"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert binding["status"] == "rejected"
    assert "statement_authority_key_unknown" in errors
    assert binding["statement_reviews"][0]["validation_errors"] == [
        "statement_authority_key_unknown"
    ]


def test_strength_uses_the_authority_that_owns_the_specific_statement() -> None:
    narrative = "当前高强度证据支持单笔付费金额是主要贡献项。"

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(narrative, "verified_claim", "结论1", "结论2"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert errors == ()
    assert binding["status"] == "bound"


def test_legal_statement_class_difference_is_normalized_and_audited() -> None:
    narrative = "单笔付费金额贡献126.2%。"

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(narrative, "data_boundary", "结论1"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert errors == ()
    assert binding["status"] == "bound"
    assert binding["statement_class_audits"] == [
        {
            "statement_index": 0,
            "submitted_class": "data_boundary",
            "normalized_class": "factor_contribution",
        }
    ]


def test_local_binding_failure_keeps_supported_statement_identifiable() -> None:
    supported = "目标日付费金额较基线日上涨1.35%"
    unsupported = "另一个地区上涨50%"
    narrative = f"{supported}。{unsupported}。"

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(supported, "verified_claim", "结论2"),
            _statement(unsupported, "verified_claim", "结论2"),
        ),
        authority_record=_multi_claim_authority(),
        quality_gate=_quality_gate(),
    )

    assert "unsupported_narrative_number" in errors
    assert binding["status"] == "rejected"
    assert binding["accepted_statement_indexes"] == [0]
    assert binding["rejected_statement_indexes"] == [1]
    assert binding["statement_reviews"][0]["status"] == "bound"
    assert binding["statement_reviews"][1]["validation_errors"] == [
        "unsupported_narrative_number"
    ]


def test_derived_counterfactual_with_complete_sources_is_publishable() -> None:
    portfolio = {
        "counterfactuals": [
            {
                "counterfactual_type": "accounting_component_removal",
                "evidence_state": "derived",
                "removed_factor_id": "avg_order_amount",
                "removed_factor": "单笔付费金额",
                "observed_change": 4_097_679.0,
                "removed_contribution": 5_172_408.34,
                "change_without_factor": -1_074_729.34,
                "direction_without_factor": "decrease",
                "derivation": "observed_change_minus_contribution",
                "source_evidence_refs": [
                    "evidence:formula-contribution",
                    "evidence:paid-amount-change",
                ],
            }
        ]
    }
    narrative = (
        "若移除单笔付费金额的贡献，付费金额将下降1,074,729.34。"
    )

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(narrative, "verified_claim", "洞察1"),
        ),
        authority_record=_multi_claim_authority(
            diagnostic_insights=portfolio,
        ),
        quality_gate=_quality_gate(),
    )

    assert errors == ()
    assert binding["status"] == "bound"


def test_candidate_insight_never_receives_publication_authority() -> None:
    portfolio = {
        "counterfactuals": [
            {
                "evidence_state": "candidate",
                "statement": "某个活动可能带来上涨。",
                "source_evidence_refs": ["evidence:paid-amount-change"],
            }
        ]
    }
    narrative = "某个活动可能带来上涨。"

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(narrative, "verified_claim", "洞察1"),
        ),
        authority_record=_multi_claim_authority(
            diagnostic_insights=portfolio,
        ),
        quality_gate=_quality_gate(),
    )

    assert binding["status"] == "rejected"
    assert "statement_authority_key_unknown" in errors


def test_derived_counterfactual_direction_cannot_be_reversed() -> None:
    portfolio = {
        "counterfactuals": [
            {
                "counterfactual_type": "accounting_component_removal",
                "evidence_state": "derived",
                "removed_factor": "单笔付费金额",
                "change_without_factor": -1_074_729.34,
                "direction_without_factor": "decrease",
                "source_evidence_refs": [
                    "evidence:formula-contribution",
                    "evidence:paid-amount-change",
                ],
            }
        ]
    }
    narrative = "若移除单笔付费金额的贡献，付费金额将上涨1,074,729.34。"

    binding, errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=(
            _statement(narrative, "verified_claim", "洞察1"),
        ),
        authority_record=_multi_claim_authority(
            diagnostic_insights=portfolio,
        ),
        quality_gate=_quality_gate(),
    )

    assert binding["status"] == "rejected"
    assert "comparison_direction_mismatch" in errors


def test_derived_insight_with_missing_source_never_receives_authority() -> None:
    portfolio = {
        "counterfactuals": [
            {
                "evidence_state": "derived",
                "change_without_factor": -999.0,
                "direction_without_factor": "decrease",
                "source_evidence_refs": ["evidence:missing"],
            }
        ]
    }
    record = _multi_claim_authority(diagnostic_insights=portfolio)

    assert record["diagnostic_insights"] == []


def test_diagnostic_insight_keys_are_stable_across_input_order() -> None:
    first = {
        "evidence_state": "verified",
        "insight_type": "dominant_driver",
        "factor": "单笔付费金额",
        "contribution": 5_172_408.34,
        "source_evidence_refs": ["evidence:formula-contribution"],
    }
    second = {
        "evidence_state": "derived",
        "counterfactual_type": "accounting_component_removal",
        "removed_factor": "单笔付费金额",
        "change_without_factor": -1_074_729.34,
        "direction_without_factor": "decrease",
        "source_evidence_refs": [
            "evidence:formula-contribution",
            "evidence:paid-amount-change",
        ],
    }

    forward = _multi_claim_authority(
        diagnostic_insights={
            "insights": [first],
            "counterfactuals": [second],
        }
    )
    reversed_order = _multi_claim_authority(
        diagnostic_insights={
            "counterfactuals": [second],
            "insights": [first],
        }
    )

    assert forward["diagnostic_insights"] == reversed_order[
        "diagnostic_insights"
    ]
