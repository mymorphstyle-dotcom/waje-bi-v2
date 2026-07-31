from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from waje_vnext.domain.measurement import ClaimStrengthCeiling


QuestionFamily: TypeAlias = Literal[
    "payment_change",
    "recurring_pattern",
    "event_impact",
    "revenue_health",
    "factor_attribution",
    "anomaly_review",
    "baseline_comparison",
    "data_quality",
]
WorldVariant: TypeAlias = Literal[
    "supported",
    "partial_gap",
    "reversal_or_conflict",
]
ClaimDisposition: TypeAlias = Literal[
    "supported_provisional",
    "bounded_provisional",
    "typed_boundary",
    "revoked",
    "unverifiable",
]
EvidenceRelationKind: TypeAlias = Literal[
    "supports",
    "qualifies",
    "contradicts",
    "invalidates",
]
RealmTag: TypeAlias = Literal[
    "conformance_only",
    "production_provenance_required",
    "claim_scoped_mixed_sources",
    "realm_is_system_issued",
    "cross_realm_use_forbidden",
]
DriftTag: TypeAlias = Literal[
    "question_frame_plan_heads",
    "comparison_window_identity",
    "exposure_definition",
    "scope_grain_unit",
    "evidence_validity",
    "answer_evidence_binding",
    "reviewed_answer_version",
    "correction_epoch",
]
CrashTag: TypeAlias = Literal[
    "result_receipt_before_evidence",
    "evidence_before_admission",
    "admission_before_satisfaction",
    "satisfaction_before_answer",
    "answer_before_review",
    "review_before_settlement_report",
    "settlement_report_before_projection",
    "projection_before_outbox_ack",
]
RaceTag: TypeAlias = Literal[
    "parallel_obligations_out_of_order",
    "correction_vs_effect",
    "correction_vs_review",
    "correction_vs_answer",
    "validity_vs_publication",
]
MutationKind: TypeAlias = Literal[
    "base",
    "local_contract_or_coverage_gap",
    "sensitivity_or_counterevidence",
]


@dataclass(frozen=True, slots=True)
class ClaimTarget:
    claim_id: str
    business_meaning: str
    strength_ceiling: ClaimStrengthCeiling
    material: bool = True


@dataclass(frozen=True, slots=True)
class EvidenceRelation:
    relation_id: str
    kind: EvidenceRelationKind
    claim_ids: tuple[str, ...]
    business_observation: str


@dataclass(frozen=True, slots=True)
class ClaimExpectation:
    claim_id: str
    allowed_dispositions: tuple[ClaimDisposition, ...]


@dataclass(frozen=True, slots=True)
class SiblingMutation:
    sibling_group: str
    base_world_id: str
    kind: MutationKind
    changed_business_facts: tuple[str, ...]
    stable_properties: tuple[str, ...]
    expected_changed_properties: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BusinessWorld:
    world_id: str
    family: QuestionFamily
    variant: WorldVariant
    user_question: str
    decision_stake: str
    claim_targets: tuple[ClaimTarget, ...]
    evidence_relations: tuple[EvidenceRelation, ...]
    claim_expectations: tuple[ClaimExpectation, ...]
    forbidden_outcomes: tuple[str, ...]
    realm_tags: tuple[RealmTag, ...]
    drift_tags: tuple[DriftTag, ...]
    crash_tags: tuple[CrashTag, ...]
    race_tags: tuple[RaceTag, ...]
    open_design_dimensions: tuple[str, ...]
    sibling_mutation: SiblingMutation


@dataclass(frozen=True, slots=True)
class _SupportGroup:
    claim_ids: tuple[str, ...]
    business_observation: str


_STABLE_SIBLING_PROPERTIES = (
    "user_question",
    "decision_target",
    "claim_target_identity",
    "requested_business_scope",
)

_OPEN_DESIGN_DIMENSIONS = (
    "measurement_window_when_not_user_fixed",
    "comparison_or_estimator",
    "investigation_order",
    "evidence_gathering_route",
    "driver_ranking",
)

_COMMON_FORBIDDEN_OUTCOMES = (
    "settled_answer",
    "delivered_answer",
    "completed_workflow_from_execution_success",
    "scope_or_strength_overreach",
    "authority_drift_acceptance",
    "cross_realm_evidence_use",
    "fixed_window_required",
    "fixed_query_or_capability_route_required",
    "fixed_driver_ranking_required",
    "omitted_material_claim",
)


def _claim(
    claim_id: str,
    business_meaning: str,
    strength_ceiling: ClaimStrengthCeiling,
) -> ClaimTarget:
    return ClaimTarget(
        claim_id=claim_id,
        business_meaning=business_meaning,
        strength_ceiling=strength_ceiling,
    )


def _support(
    claim_ids: tuple[str, ...],
    business_observation: str,
) -> _SupportGroup:
    return _SupportGroup(
        claim_ids=claim_ids,
        business_observation=business_observation,
    )


def _expectations(
    claims: tuple[ClaimTarget, ...],
    overrides: tuple[
        tuple[str, tuple[ClaimDisposition, ...]], ...
    ] = (),
) -> tuple[ClaimExpectation, ...]:
    override_by_claim = dict(overrides)
    results = []
    for claim in claims:
        allowed = override_by_claim.get(
            claim.claim_id,
            ("supported_provisional",),
        )
        if (
            allowed != ("supported_provisional",)
            and "typed_boundary" not in allowed
            and "unverifiable" not in allowed
        ):
            # G3.5 has no trusted persisted falsification/reversal execution
            # authority. Until G3.6/Gate 5 supplies one, a counterevidence
            # world must permit the affected claim to remain unverifiable.
            allowed = (*allowed, "unverifiable")
        results.append(
            ClaimExpectation(
                claim_id=claim.claim_id,
                allowed_dispositions=allowed,
            )
        )
    return tuple(results)


def _support_relations(
    *,
    world_id: str,
    groups: tuple[_SupportGroup, ...],
) -> tuple[EvidenceRelation, ...]:
    return tuple(
        EvidenceRelation(
            relation_id=f"{world_id}:support:{index}",
            kind="supports",
            claim_ids=group.claim_ids,
            business_observation=group.business_observation,
        )
        for index, group in enumerate(groups, start=1)
    )


def _triad(
    *,
    family: QuestionFamily,
    user_question: str,
    decision_stake: str,
    claims: tuple[ClaimTarget, ...],
    support_groups: tuple[_SupportGroup, ...],
    partial_overrides: tuple[
        tuple[str, tuple[ClaimDisposition, ...]], ...
    ],
    partial_boundary_claim_ids: tuple[str, ...],
    partial_invalidated_claim_ids: tuple[str, ...],
    partial_fact: str,
    conflict_overrides: tuple[
        tuple[str, tuple[ClaimDisposition, ...]], ...
    ],
    conflict_claim_ids: tuple[str, ...],
    conflict_fact: str,
    family_forbidden_outcomes: tuple[str, ...],
) -> tuple[BusinessWorld, BusinessWorld, BusinessWorld]:
    sibling_group = f"g3.5:{family}"
    base_world_id = f"{sibling_group}:supported"
    common = {
        "family": family,
        "user_question": user_question,
        "decision_stake": decision_stake,
        "claim_targets": claims,
        "open_design_dimensions": _OPEN_DESIGN_DIMENSIONS,
    }

    supported = BusinessWorld(
        world_id=base_world_id,
        variant="supported",
        evidence_relations=_support_relations(
            world_id=base_world_id,
            groups=support_groups,
        ),
        claim_expectations=_expectations(claims),
        forbidden_outcomes=(
            *_COMMON_FORBIDDEN_OUTCOMES,
            *family_forbidden_outcomes,
        ),
        realm_tags=(
            "conformance_only",
            "realm_is_system_issued",
        ),
        drift_tags=(
            "question_frame_plan_heads",
            "comparison_window_identity",
            "exposure_definition",
            "answer_evidence_binding",
        ),
        crash_tags=(
            "result_receipt_before_evidence",
            "answer_before_review",
            "settlement_report_before_projection",
        ),
        race_tags=("parallel_obligations_out_of_order",),
        sibling_mutation=SiblingMutation(
            sibling_group=sibling_group,
            base_world_id=base_world_id,
            kind="base",
            changed_business_facts=(),
            stable_properties=_STABLE_SIBLING_PROPERTIES,
            expected_changed_properties=(),
        ),
        **common,
    )

    partial_world_id = f"{sibling_group}:partial-gap"
    if set(partial_boundary_claim_ids) & set(partial_invalidated_claim_ids):
        raise ValueError("a partial claim cannot be both boundary and invalidated")
    partial_relations: list[EvidenceRelation] = []
    if partial_boundary_claim_ids:
        partial_relations.append(
            EvidenceRelation(
                relation_id=f"{partial_world_id}:qualification",
                kind="qualifies",
                claim_ids=partial_boundary_claim_ids,
                business_observation=partial_fact,
            )
        )
    if partial_invalidated_claim_ids:
        partial_relations.append(
            EvidenceRelation(
                relation_id=f"{partial_world_id}:invalidation",
                kind="invalidates",
                claim_ids=partial_invalidated_claim_ids,
                business_observation=partial_fact,
            )
        )
    partial = BusinessWorld(
        world_id=partial_world_id,
        variant="partial_gap",
        evidence_relations=(
            *_support_relations(
                world_id=partial_world_id,
                groups=support_groups,
            ),
            *partial_relations,
        ),
        claim_expectations=_expectations(claims, partial_overrides),
        forbidden_outcomes=(
            *_COMMON_FORBIDDEN_OUTCOMES,
            "global_degradation_from_local_gap",
            "hidden_material_data_gap",
            *family_forbidden_outcomes,
        ),
        realm_tags=(
            "conformance_only",
            "claim_scoped_mixed_sources",
            "realm_is_system_issued",
        ),
        drift_tags=(
            "scope_grain_unit",
            "evidence_validity",
            "answer_evidence_binding",
            "correction_epoch",
        ),
        crash_tags=(
            "evidence_before_admission",
            "admission_before_satisfaction",
            "projection_before_outbox_ack",
        ),
        race_tags=(
            "correction_vs_effect",
            "validity_vs_publication",
        ),
        sibling_mutation=SiblingMutation(
            sibling_group=sibling_group,
            base_world_id=base_world_id,
            kind="local_contract_or_coverage_gap",
            changed_business_facts=(partial_fact,),
            stable_properties=_STABLE_SIBLING_PROPERTIES,
            expected_changed_properties=(
                "affected_claim_disposition",
                "affected_obligation_state",
                "answer_limitations",
            ),
        ),
        **common,
    )

    conflict_world_id = f"{sibling_group}:reversal-or-conflict"
    conflict = BusinessWorld(
        world_id=conflict_world_id,
        variant="reversal_or_conflict",
        evidence_relations=(
            *_support_relations(
                world_id=conflict_world_id,
                groups=support_groups,
            ),
            EvidenceRelation(
                relation_id=f"{conflict_world_id}:counterevidence",
                kind="contradicts",
                claim_ids=conflict_claim_ids,
                business_observation=conflict_fact,
            ),
        ),
        claim_expectations=_expectations(claims, conflict_overrides),
        forbidden_outcomes=(
            *_COMMON_FORBIDDEN_OUTCOMES,
            "hidden_contradiction_or_reversal",
            "stale_evidence_reuse",
            *family_forbidden_outcomes,
        ),
        realm_tags=(
            "conformance_only",
            "realm_is_system_issued",
        ),
        drift_tags=(
            "evidence_validity",
            "answer_evidence_binding",
            "reviewed_answer_version",
            "correction_epoch",
        ),
        crash_tags=(
            "satisfaction_before_answer",
            "review_before_settlement_report",
        ),
        race_tags=(
            "correction_vs_review",
            "correction_vs_answer",
        ),
        sibling_mutation=SiblingMutation(
            sibling_group=sibling_group,
            base_world_id=base_world_id,
            kind="sensitivity_or_counterevidence",
            changed_business_facts=(conflict_fact,),
            stable_properties=_STABLE_SIBLING_PROPERTIES,
            expected_changed_properties=(
                "contradiction_or_reversal_status",
                "affected_claim_disposition",
                "answer_version",
            ),
        ),
        **common,
    )
    return supported, partial, conflict


BUSINESS_WORLDS: tuple[BusinessWorld, ...] = (
    *_triad(
        family="payment_change",
        user_question=(
            "昨天付费金额为什么变化，主要由首充人数、付费频次、"
            "单笔金额还是支付成功率带动？"
        ),
        decision_stake="判断收入变化来自业务需求、用户结构还是支付过程。",
        claims=(
            _claim(
                "payment_change.amount_direction",
                "目标业务日付费金额相对已接受基准的方向与幅度。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "payment_change.behavior_bridge",
                "首充人数、频次和单笔金额对变化的可重算分解。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "payment_change.payment_success",
                "支付成功率变化与付费金额变化的关系。",
                ClaimStrengthCeiling.ASSOCIATIONAL,
            ),
            _claim(
                "payment_change.driver_summary",
                "在可用证据范围内最能解释变化的业务因素。",
                ClaimStrengthCeiling.ASSOCIATIONAL,
            ),
        ),
        support_groups=(
            _support(
                (
                    "payment_change.amount_direction",
                    "payment_change.behavior_bridge",
                ),
                "完整业务日的付费金额与可核对的行为分解共同可用。",
            ),
            _support(
                ("payment_change.payment_success",),
                "支付尝试和成功状态在同一业务日边界内完整可比。",
            ),
            _support(
                ("payment_change.driver_summary",),
                "各候选因素的变化、贡献和残差都可观察。",
            ),
        ),
        partial_overrides=(
            (
                "payment_change.payment_success",
                ("typed_boundary",),
            ),
            (
                "payment_change.driver_summary",
                ("bounded_provisional",),
            ),
        ),
        partial_boundary_claim_ids=("payment_change.payment_success",),
        partial_invalidated_claim_ids=(
            "payment_change.driver_summary",
        ),
        partial_fact="部分支付尝试缺少终态，成功率口径无法完整闭合。",
        conflict_overrides=(
            (
                "payment_change.amount_direction",
                ("bounded_provisional",),
            ),
            (
                "payment_change.driver_summary",
                ("bounded_provisional", "revoked"),
            ),
        ),
        conflict_claim_ids=(
            "payment_change.amount_direction",
            "payment_change.driver_summary",
        ),
        conflict_fact=(
            "另一个业务上合理的基准给出相反方向，原主结论缺少稳健性。"
        ),
        family_forbidden_outcomes=(
            "unreconciled_or_double_counted_bridge",
            "raw_total_direction_from_incomparable_exposure",
        ),
    ),
    *_triad(
        family="recurring_pattern",
        user_question=(
            "最近付费金额有没有稳定规律，主要由哪些渠道、地区、"
            "用户类型或玩法带动？"
        ),
        decision_stake="区分可复用经营规律与一次性波动。",
        claims=(
            _claim(
                "recurring_pattern.pattern_existence",
                "付费金额是否存在重复出现的时间规律。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "recurring_pattern.pattern_stability",
                "规律在未用于发现的时间段中是否仍保持。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "recurring_pattern.segment_driver",
                "渠道、地区或用户类型与规律的关系。",
                ClaimStrengthCeiling.ASSOCIATIONAL,
            ),
            _claim(
                "recurring_pattern.intraday_or_gameplay",
                "日内时段或玩法是否带动该规律。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
        ),
        support_groups=(
            _support(
                (
                    "recurring_pattern.pattern_existence",
                    "recurring_pattern.pattern_stability",
                ),
                "足够长且覆盖一致的历史与留出时段均可观察。",
            ),
            _support(
                ("recurring_pattern.segment_driver",),
                "可比较的渠道、地区和用户类型构成可观察。",
            ),
            _support(
                ("recurring_pattern.intraday_or_gameplay",),
                "业务时区下的日内和玩法证据在所声明范围内可用。",
            ),
        ),
        partial_overrides=(
            (
                "recurring_pattern.intraday_or_gameplay",
                ("typed_boundary",),
            ),
        ),
        partial_boundary_claim_ids=(
            "recurring_pattern.intraday_or_gameplay",
        ),
        partial_invalidated_claim_ids=(),
        partial_fact="本地时段或玩法归属合同缺失，日级规律仍可独立判断。",
        conflict_overrides=(
            (
                "recurring_pattern.pattern_existence",
                ("bounded_provisional", "revoked"),
            ),
            (
                "recurring_pattern.pattern_stability",
                ("bounded_provisional", "revoked"),
            ),
        ),
        conflict_claim_ids=(
            "recurring_pattern.pattern_existence",
            "recurring_pattern.pattern_stability",
        ),
        conflict_fact="候选规律在未参与发现的留出时段中反转或消失。",
        family_forbidden_outcomes=(
            "fixed_pattern_without_holdout_support",
            "confounder_blind_pattern_claim",
        ),
    ),
    *_triad(
        family="event_impact",
        user_question=(
            "活动、投放、素材、版本、支付通道、节日或外部事件"
            "是否影响了昨天的付费金额？"
        ),
        decision_stake="区分可行动的内部事件影响与只能作为背景的共时变化。",
        claims=(
            _claim(
                "event_impact.payment_incident",
                "支付通道事件对支付过程的直接影响。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "event_impact.campaign_budget_joint",
                "重叠活动和预算暴露的联合影响。",
                ClaimStrengthCeiling.ASSOCIATIONAL,
            ),
            _claim(
                "event_impact.creative_or_version",
                "素材或版本变化对覆盖人群的因果影响。",
                ClaimStrengthCeiling.CAUSAL,
            ),
            _claim(
                "event_impact.external_context",
                "节日或外部事件与收入变化的背景关系。",
                ClaimStrengthCeiling.ASSOCIATIONAL,
            ),
        ),
        support_groups=(
            _support(
                ("event_impact.payment_incident",),
                "支付事件时间线与支付过程指标在同一边界内可观察。",
            ),
            _support(
                ("event_impact.campaign_budget_joint",),
                "活动和预算的重叠暴露作为联合处理可观察。",
            ),
            _support(
                ("event_impact.creative_or_version",),
                "覆盖人群存在可信分配或可辩护的对照设计。",
            ),
            _support(
                ("event_impact.external_context",),
                "外部事件与业务时间线可对齐，但仅支持声明范围。",
            ),
        ),
        partial_overrides=(
            (
                "event_impact.creative_or_version",
                ("typed_boundary",),
            ),
            (
                "event_impact.external_context",
                ("bounded_provisional",),
            ),
        ),
        partial_boundary_claim_ids=(
            "event_impact.creative_or_version",
        ),
        partial_invalidated_claim_ids=(
            "event_impact.external_context",
        ),
        partial_fact="素材或版本覆盖缺少可信分配，外部事件也没有识别设计。",
        conflict_overrides=(
            (
                "event_impact.campaign_budget_joint",
                ("bounded_provisional", "revoked"),
            ),
        ),
        conflict_claim_ids=("event_impact.campaign_budget_joint",),
        conflict_fact=(
            "控制同期构成变化后，活动和预算的原关联方向反转。"
        ),
        family_forbidden_outcomes=(
            "overlapping_events_reported_as_additive_causal_effects",
            "contextual_event_presented_as_identified_cause",
        ),
    ),
    *_triad(
        family="revenue_health",
        user_question=(
            "当前收入健康吗，是正常用户增长带动，还是少数大额用户、"
            "短期活动或异常渠道拉动？最大风险是什么？"
        ),
        decision_stake="判断收入质量、依赖性和不同经营期限的风险。",
        claims=(
            _claim(
                "revenue_health.payer_growth",
                "收入增长是否伴随可持续的付费用户扩展。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "revenue_health.concentration",
                "收入对少数用户、渠道或细分群体的集中风险。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "revenue_health.activity_dependency",
                "收入与短期活动暴露的依赖关系。",
                ClaimStrengthCeiling.ASSOCIATIONAL,
            ),
            _claim(
                "revenue_health.revenue_quality",
                "退款、冲正和收入成熟度约束下的收入质量。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
        ),
        support_groups=(
            _support(
                ("revenue_health.payer_growth",),
                "可比较期限内的付费用户和收入扩展均可观察。",
            ),
            _support(
                ("revenue_health.concentration",),
                "聚合后仍满足安全边界的用户和业务切片集中度可用。",
            ),
            _support(
                ("revenue_health.activity_dependency",),
                "活动暴露与收入结构在声明期限内可比较。",
            ),
            _support(
                ("revenue_health.revenue_quality",),
                "退款、冲正和收入成熟状态覆盖完整。",
            ),
        ),
        partial_overrides=(
            (
                "revenue_health.revenue_quality",
                ("typed_boundary",),
            ),
        ),
        partial_boundary_claim_ids=("revenue_health.revenue_quality",),
        partial_invalidated_claim_ids=(),
        partial_fact="退款或收入成熟状态覆盖不足，其他结构指标仍可独立使用。",
        conflict_overrides=(
            (
                "revenue_health.payer_growth",
                ("bounded_provisional",),
            ),
            (
                "revenue_health.activity_dependency",
                ("bounded_provisional",),
            ),
        ),
        conflict_claim_ids=(
            "revenue_health.payer_growth",
            "revenue_health.activity_dependency",
        ),
        conflict_fact="短期增长健康，但更长期限显示活动依赖和集中度恶化。",
        family_forbidden_outcomes=(
            "single_universal_health_score",
            "healthy_synthesis_hiding_material_quality_gap",
        ),
    ),
    *_triad(
        family="factor_attribution",
        user_question=(
            "昨天收入变化最大的渠道、地区、设备、包、支付方式或玩法"
            "是什么，对收入影响最大的因素有哪些？"
        ),
        decision_stake="定位变化所在业务切片，并区分位置贡献与行为机制。",
        claims=(
            _claim(
                "factor_attribution.dimension_bridges",
                "各业务维度内部可重算的收入变化贡献。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "factor_attribution.behavior_bridge",
                "人数、频次、单笔金额等行为因素的可重算贡献。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "factor_attribution.driver_ranking",
                "在所选可比因素范围内的主要驱动排序。",
                ClaimStrengthCeiling.ASSOCIATIONAL,
            ),
            _claim(
                "factor_attribution.gameplay",
                "玩法与收入变化的关系。",
                ClaimStrengthCeiling.ASSOCIATIONAL,
            ),
        ),
        support_groups=(
            _support(
                ("factor_attribution.dimension_bridges",),
                "每个维度都能独立闭合到相同的总变化和显式残差。",
            ),
            _support(
                (
                    "factor_attribution.behavior_bridge",
                    "factor_attribution.driver_ranking",
                ),
                "行为因素桥接可重算且候选因素证据可比较。",
            ),
            _support(
                ("factor_attribution.gameplay",),
                "玩法归属与收入观察单位在同一声明范围内可用。",
            ),
        ),
        partial_overrides=(
            (
                "factor_attribution.gameplay",
                ("typed_boundary",),
            ),
        ),
        partial_boundary_claim_ids=("factor_attribution.gameplay",),
        partial_invalidated_claim_ids=(),
        partial_fact="玩法归属合同缺失，维度桥接和行为桥接仍然闭合。",
        conflict_overrides=(
            (
                "factor_attribution.driver_ranking",
                ("bounded_provisional", "revoked"),
            ),
        ),
        conflict_claim_ids=("factor_attribution.driver_ranking",),
        conflict_fact=(
            "替代但同样可辩护的构成控制改变了主要驱动的排序。"
        ),
        family_forbidden_outcomes=(
            "overlapping_dimension_bridges_added_together",
            "driver_rank_without_reconciled_residual",
        ),
    ),
    *_triad(
        family="anomaly_review",
        user_question=(
            "昨天有没有异常波动，如果有，是哪个渠道、支付通道、"
            "地区、设备、玩法或大额用户造成的？"
        ),
        decision_stake="区分过程事故、全日残差异常、异常定位和根因。",
        claims=(
            _claim(
                "anomaly_review.process_anomaly",
                "业务日内是否发生可核验的过程异常。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "anomaly_review.full_day_residual",
                "控制正常变化后全日结果是否仍异常。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "anomaly_review.localization",
                "异常集中在哪些业务切片。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "anomaly_review.root_cause",
                "异常由某个业务机制导致的因果解释。",
                ClaimStrengthCeiling.CAUSAL,
            ),
        ),
        support_groups=(
            _support(
                ("anomaly_review.process_anomaly",),
                "过程事件与受影响指标的时间关系可核验。",
            ),
            _support(
                ("anomaly_review.full_day_residual",),
                "目标日与可辩护正常范围具有完整可比 exposure。",
            ),
            _support(
                ("anomaly_review.localization",),
                "渠道、支付、地区、设备和安全聚合用户切片可观察。",
            ),
            _support(
                ("anomaly_review.root_cause",),
                "机制证据能够排除关键替代解释并支持因果强度。",
            ),
        ),
        partial_overrides=(
            (
                "anomaly_review.root_cause",
                ("typed_boundary",),
            ),
        ),
        partial_boundary_claim_ids=("anomaly_review.root_cause",),
        partial_invalidated_claim_ids=(),
        partial_fact="能定位异常切片，但缺少区分定位与根因的机制证据。",
        conflict_overrides=(
            (
                "anomaly_review.full_day_residual",
                ("bounded_provisional", "revoked"),
            ),
        ),
        conflict_claim_ids=("anomaly_review.full_day_residual",),
        conflict_fact=(
            "过程故障确实发生，但恢复后全日残差落回正常范围。"
        ),
        family_forbidden_outcomes=(
            "localized_segment_presented_as_proven_root_cause",
            "recovered_process_incident_erased_from_history",
        ),
    ),
    *_triad(
        family="baseline_comparison",
        user_question=(
            "相比前一天、用户指定的滚动均值和上周同日，昨天付费金额"
            "为什么变化，哪些指标偏离正常水平？"
        ),
        decision_stake="同时保留多个经营基准，解释各自方向与正常性。",
        claims=(
            _claim(
                "baseline_comparison.prior_period",
                "目标业务日相对前一可比业务日的变化。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "baseline_comparison.rolling_baseline",
                "目标业务日相对用户请求的滚动基准的变化。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "baseline_comparison.weekday_baseline",
                "目标业务日相对上一个同类星期位置的变化。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "baseline_comparison.conditional_range",
                "目标业务日在条件正常范围中的偏离。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "baseline_comparison.driver_by_contrast",
                "每个独立对比下的变化驱动。",
                ClaimStrengthCeiling.ASSOCIATIONAL,
            ),
        ),
        support_groups=(
            _support(
                ("baseline_comparison.prior_period",),
                "前一可比业务日及其完整 exposure 可用。",
            ),
            _support(
                ("baseline_comparison.rolling_baseline",),
                "用户请求的滚动基准由完整可比观察构成。",
            ),
            _support(
                ("baseline_comparison.weekday_baseline",),
                "同类星期位置的业务日完整可比。",
            ),
            _support(
                ("baseline_comparison.conditional_range",),
                "条件正常范围所需的业务状态与覆盖完整。",
            ),
            _support(
                ("baseline_comparison.driver_by_contrast",),
                "每个对比拥有独立闭合的驱动桥接。",
            ),
        ),
        partial_overrides=(
            (
                "baseline_comparison.conditional_range",
                ("typed_boundary",),
            ),
        ),
        partial_boundary_claim_ids=(
            "baseline_comparison.conditional_range",
        ),
        partial_invalidated_claim_ids=(),
        partial_fact="活动调整所需合同缺失，三个明确基准仍可分别回答。",
        conflict_overrides=(
            (
                "baseline_comparison.driver_by_contrast",
                ("bounded_provisional",),
            ),
        ),
        conflict_claim_ids=(
            "baseline_comparison.driver_by_contrast",
        ),
        conflict_fact=(
            "三个独立基准给出冲突方向，且各自的主要驱动不同。"
        ),
        family_forbidden_outcomes=(
            "conflicting_baseline_hidden_or_averaged_away",
            "one_bridge_reused_for_distinct_contrasts",
        ),
    ),
    *_triad(
        family="data_quality",
        user_question=(
            "这个结论的数据证据够不够，是否受延迟、归因异常、"
            "支付状态缺失、重复订单或异常用户影响？"
        ),
        decision_stake="判断每条业务结论能否继续使用、降级、撤销或重算。",
        claims=(
            _claim(
                "data_quality.evidence_sufficiency",
                "每条业务结论的证据完整性与适用边界。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "data_quality.paid_amount_conclusion",
                "付费金额变化结论在当前有效证据下是否成立。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
            _claim(
                "data_quality.channel_attribution",
                "渠道归因结论在当前有效证据下是否成立。",
                ClaimStrengthCeiling.ASSOCIATIONAL,
            ),
            _claim(
                "data_quality.historical_provenance",
                "历史结论能否追溯到不可变来源和权威版本。",
                ClaimStrengthCeiling.DESCRIPTIVE,
            ),
        ),
        support_groups=(
            _support(
                (
                    "data_quality.evidence_sufficiency",
                    "data_quality.paid_amount_conclusion",
                ),
                "付费汇总及其 coverage、状态和去重依据均可追溯。",
            ),
            _support(
                ("data_quality.channel_attribution",),
                "渠道归属完整且与订单观察单位一致。",
            ),
            _support(
                ("data_quality.historical_provenance",),
                "历史结论保留原 Evidence、authority heads 和内容哈希。",
            ),
        ),
        partial_overrides=(
            (
                "data_quality.historical_provenance",
                ("unverifiable",),
            ),
        ),
        partial_boundary_claim_ids=(),
        partial_invalidated_claim_ids=(
            "data_quality.historical_provenance",
        ),
        partial_fact=(
            "一条历史结论只剩摘要，没有可恢复的来源和权威引用。"
        ),
        conflict_overrides=(
            (
                "data_quality.paid_amount_conclusion",
                ("revoked",),
            ),
            (
                "data_quality.channel_attribution",
                ("bounded_provisional", "revoked"),
            ),
        ),
        conflict_claim_ids=(
            "data_quality.paid_amount_conclusion",
            "data_quality.channel_attribution",
        ),
        conflict_fact=(
            "重复订单或归因修复使旧 Evidence 失效，并改变依赖结论。"
        ),
        family_forbidden_outcomes=(
            "missing_historical_provenance_recreated_from_current_aggregate",
            "revoked_claim_left_current_or_completed",
        ),
    ),
)


def worlds_for_family(
    family: QuestionFamily,
) -> tuple[BusinessWorld, ...]:
    return tuple(
        world for world in BUSINESS_WORLDS if world.family == family
    )
