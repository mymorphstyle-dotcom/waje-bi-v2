from collections.abc import Iterable, Mapping
from typing import Any, Optional

from bi_agent.runtime.models import (
    CompiledGraph,
    GraphNode,
    MutationLedger,
    MutationRecord,
    RecipeEntry,
)
from bi_agent.runtime.capability_registry import get_capability_card, public_capability_ids
from bi_agent.runtime.recipe_registry import load_recipe_registry
from bi_agent.runtime.revenue_runtime_plan import build_revenue_runtime_plan


REQUIRED_PATTERN_PATHS = (
    "data_quality_check",
    "pattern_scan",
    "formula_decompose",
    "event_evidence",
    "segment_bridge",
    "outlier_scan",
    "answer_verify",
)

SUPPORTED_CAPABILITIES = frozenset(
    (*REQUIRED_PATTERN_PATHS, "joint_attribution", *public_capability_ids())
)
PUBLIC_CAPABILITIES = frozenset(public_capability_ids())
PHASE6_ENABLED_FAMILY_REQUIREMENTS = {
    "paid_amount_change_explanation": frozenset(("driver_decomposition",)),
    "segment_or_factor_attribution": frozenset(("segment_contribution",)),
    "anomaly_or_black_swan_review": frozenset(("outlier_contribution",)),
    "data_quality_or_evidence_review": frozenset(("data_quality_profile",)),
    "revenue_health_review": frozenset(
        (
            "data_quality_profile",
            "driver_decomposition",
            "user_mix_contribution",
            "high_value_user_contribution",
            "outlier_scan",
            "event_evidence",
            "answer_verify",
        )
    ),
    "business_object_impact_review": frozenset(("event_evidence", "answer_verify")),
}
REVENUE_DIAGNOSTIC_BUNDLES = {
    "driver_focus": (
        "data_quality_profile",
        "driver_decomposition",
        "answer_verify",
    ),
    "change_explanation": (
        "data_quality_profile",
        "compare_periods",
        "driver_decomposition",
        "answer_verify",
    ),
    "pattern_attribution": (
        "data_quality_profile",
        "pattern_scan",
        "segment_contribution",
        "joint_attribution",
        "answer_verify",
    ),
    "event_impact": (
        "data_quality_profile",
        "compare_periods",
        "event_evidence",
        "answer_verify",
    ),
    "revenue_health": (
        "data_quality_profile",
        "driver_decomposition",
        "user_mix_contribution",
        "high_value_user_contribution",
        "outlier_scan",
        "event_evidence",
        "answer_verify",
    ),
    "factor_topk": (
        "data_quality_profile",
        "segment_contribution",
        "joint_attribution",
        "driver_decomposition",
        "answer_verify",
    ),
    "anomaly": (
        "outlier_scan",
        "outlier_contribution",
        "segment_contribution",
        "joint_attribution",
        "high_value_user_contribution",
        "answer_verify",
    ),
    "multi_baseline": (
        "data_quality_profile",
        "compare_periods",
        "rolling_window_compare",
        "driver_decomposition",
        "answer_verify",
    ),
    "evidence_quality": (
        "data_quality_profile",
        "segment_contribution",
        "joint_attribution",
        "outlier_scan",
        "answer_verify",
    ),
}
REVENUE_DIAGNOSTIC_FAMILIES = {
    "driver_focus": ("paid_amount_change_explanation",),
    "change_explanation": ("paid_amount_change_explanation", "custom_baseline_comparison"),
    "pattern_attribution": ("pattern_explanation", "segment_or_factor_attribution"),
    "event_impact": ("business_object_impact_review", "paid_amount_change_explanation"),
    "revenue_health": (
        "revenue_health_review",
        "paid_amount_change_explanation",
        "segment_or_factor_attribution",
        "anomaly_or_black_swan_review",
    ),
    "factor_topk": ("segment_or_factor_attribution", "paid_amount_change_explanation"),
    "anomaly": (
        "anomaly_or_black_swan_review",
        "segment_or_factor_attribution",
        "paid_amount_change_explanation",
    ),
    "multi_baseline": ("custom_baseline_comparison", "paid_amount_change_explanation"),
    "evidence_quality": (
        "data_quality_or_evidence_review",
        "segment_or_factor_attribution",
        "anomaly_or_black_swan_review",
    ),
}
CAPABILITY_ORDER = {
    "data_quality_check": 10,
    "data_quality_profile": 11,
    "compare_periods": 20,
    "rolling_window_compare": 21,
    "compare_period_phases": 22,
    "weekday_calendar_compare": 23,
    "pattern_scan": 30,
    "formula_decompose": 40,
    "driver_decomposition": 41,
    "segment_bridge": 50,
    "segment_contribution": 51,
    "joint_attribution": 52,
    "user_mix_contribution": 60,
    "high_value_user_contribution": 61,
    "outlier_scan": 70,
    "outlier_contribution": 71,
    "event_evidence": 80,
    "answer_verify": 90,
}


def compile_graph(
    *,
    question_family: str,
    target_metric: str,
    pattern_family: Optional[str] = None,
    requested_nodes: Iterable[str] = (),
    question_families: Iterable[str] = (),
    question_text: str = "",
    prior_analysis_assets: Iterable[Mapping[str, Any]] = (),
    registry: Optional[Mapping[str, RecipeEntry]] = None,
) -> CompiledGraph:
    registry = load_recipe_registry() if registry is None else registry
    base_proposed_graph = _dedupe(tuple(requested_nodes))
    explicit_requested = bool(base_proposed_graph)
    recipe = registry.get(question_family)

    if recipe is None:
        return _compiled(
            status="rejected",
            target_metric=target_metric,
            accepted=(),
            proposed=base_proposed_graph,
            rejected_or_degraded=(question_family,),
            records=(
                MutationRecord(
                    action="rejected",
                    capability=question_family,
                    reason="unknown_question_family",
                ),
            ),
        )

    if not base_proposed_graph:
        base_proposed_graph = recipe.subgraph_nodes

    diagnostic_axes = _revenue_diagnostic_axes(
        question_text=question_text,
        question_family=question_family,
        pattern_family=pattern_family,
        requested_nodes=base_proposed_graph,
    )
    diagnostic_nodes = _revenue_diagnostic_nodes(diagnostic_axes)
    proposed_graph = _dedupe((*base_proposed_graph, *diagnostic_nodes))

    supported_families = frozenset(
        _dedupe(
            (
                question_family,
                *tuple(question_families),
                *_revenue_diagnostic_families(diagnostic_axes),
            )
        )
    )
    unknown = tuple(node for node in proposed_graph if node not in SUPPORTED_CAPABILITIES)
    unsupported_for_family = tuple(
        node
        for node in proposed_graph
        if node in PUBLIC_CAPABILITIES
        and node not in REQUIRED_PATTERN_PATHS
        and not supported_families.intersection(get_capability_card(node).supported_question_families)
    )
    known_requested = tuple(
        node
        for node in proposed_graph
        if node in SUPPORTED_CAPABILITIES and node not in unsupported_for_family
    )
    records = tuple(
        MutationRecord(
            action="rejected",
            capability=node,
            reason="unknown_capability",
        )
        for node in unknown
    )
    records = (
        *records,
        *(
            MutationRecord(
                action="rejected",
                capability=node,
                reason="unsupported_question_family",
            )
            for node in unsupported_for_family
        ),
    )
    records = (
        *records,
        *(
            MutationRecord(
                action="auto_added",
                capability=node,
                reason=f"revenue_diagnostics:{axis}",
            )
            for axis in diagnostic_axes
            for node in REVENUE_DIAGNOSTIC_BUNDLES[axis]
            if node not in base_proposed_graph
        ),
    )
    def make_runtime_plan(accepted: tuple[str, ...]) -> dict:
        return build_revenue_runtime_plan(
            target_metric=target_metric,
            accepted_graph=accepted,
            diagnostic_axes=diagnostic_axes,
            question_text=question_text,
            prior_assets=prior_analysis_assets,
        )

    if question_family == "pattern_explanation":
        accepted = _dedupe((*known_requested, *REQUIRED_PATTERN_PATHS))
        accepted = _order_capabilities(accepted, diagnostic_axes)
        auto_added = tuple(node for node in accepted if node not in known_requested)
        records = (
            *records,
            *(
                MutationRecord(
                    action="auto_added",
                    capability=node,
                    reason=f"required_pattern_path:{pattern_family or 'unspecified'}",
                )
                for node in auto_added
            ),
        )
        return _compiled(
            status="accepted" if not unknown and not unsupported_for_family else "degraded",
            target_metric=target_metric,
            accepted=accepted,
            proposed=proposed_graph,
            rejected_or_degraded=_dedupe((*unknown, *unsupported_for_family)),
            records=records,
            runtime_plan=make_runtime_plan(accepted),
        )

    if question_family == "custom_baseline_comparison" and known_requested:
        accepted = _order_capabilities(_dedupe(known_requested), diagnostic_axes)
        return _compiled(
            status="accepted" if not unknown and not unsupported_for_family else "degraded",
            target_metric=target_metric,
            accepted=accepted,
            proposed=proposed_graph,
            rejected_or_degraded=_dedupe((*unknown, *unsupported_for_family)),
            records=records,
            runtime_plan=make_runtime_plan(accepted),
        )

    if explicit_requested and known_requested:
        accepted = _order_capabilities(_dedupe(known_requested), diagnostic_axes)
        enablement = PHASE6_ENABLED_FAMILY_REQUIREMENTS.get(question_family)
        if recipe.default_degraded and not (
            enablement and enablement.issubset(set(accepted))
        ):
            return _compiled(
                status="degraded",
                target_metric=target_metric,
                accepted=accepted,
                proposed=proposed_graph,
                rejected_or_degraded=_dedupe(
                    (*unknown, *unsupported_for_family, question_family)
                ),
                records=(
                    *records,
                    MutationRecord(
                        action="degraded",
                        capability=question_family,
                        reason="phase6_family_not_enabled",
                    ),
                ),
                node_status="degraded",
                runtime_plan=make_runtime_plan(accepted),
            )
        return _compiled(
            status="accepted" if not unknown and not unsupported_for_family else "degraded",
            target_metric=target_metric,
            accepted=accepted,
            proposed=proposed_graph,
            rejected_or_degraded=_dedupe((*unknown, *unsupported_for_family)),
            records=records,
            runtime_plan=make_runtime_plan(accepted),
        )

    accepted = _order_capabilities(
        tuple(node for node in recipe.subgraph_nodes if node in SUPPORTED_CAPABILITIES),
        diagnostic_axes,
    )
    skipped_supported = tuple(node for node in known_requested if node not in accepted)
    records = (
        *records,
        *(
            MutationRecord(
                action="degraded",
                capability=node,
                reason="non_pattern_dry_run_skeleton",
            )
            for node in accepted
        ),
        *(
            MutationRecord(
                action="degraded",
                capability=node,
                reason="non_pattern_recipe_skeleton_scope",
            )
            for node in skipped_supported
        ),
    )
    return _compiled(
        status="degraded",
        target_metric=target_metric,
        accepted=accepted,
        proposed=proposed_graph,
        rejected_or_degraded=_dedupe(
            (*unknown, *unsupported_for_family, *accepted, *skipped_supported)
        ),
        records=records,
        node_status="degraded",
        runtime_plan=make_runtime_plan(accepted),
    )


def _compiled(
    *,
    status: str,
    target_metric: str,
    accepted: tuple[str, ...],
    proposed: tuple[str, ...],
    rejected_or_degraded: tuple[str, ...],
    records: tuple[MutationRecord, ...],
    node_status: str = "accepted",
    runtime_plan: Optional[dict] = None,
) -> CompiledGraph:
    return CompiledGraph(
        status=status,
        accepted_nodes=tuple(
            GraphNode(
                node_id=f"{index:02d}_{capability}",
                capability=capability,
                status=node_status,
                target_claim=target_metric,
                depends_on=accepted[:index],
            )
            for index, capability in enumerate(accepted)
        ),
        mutations=MutationLedger(
            proposed_graph=proposed,
            accepted_graph=accepted,
            rejected_or_degraded=rejected_or_degraded,
            records=records,
        ),
        runtime_plan=runtime_plan or {},
    )


def suggest_revenue_diagnostic_nodes(question_text: str, intent: str = "") -> tuple[str, ...]:
    axes = _revenue_diagnostic_axes(
        question_text=question_text,
        question_family="",
        pattern_family=None,
        requested_nodes=(),
        conversation_intent=intent,
    )
    return _revenue_diagnostic_nodes(axes)


def _revenue_diagnostic_axes(
    *,
    question_text: str,
    question_family: str,
    pattern_family: Optional[str],
    requested_nodes: Iterable[str],
    conversation_intent: str = "",
) -> tuple[str, ...]:
    text = str(question_text or "").lower()
    requested = set(requested_nodes)
    axes: list[str] = []
    has_driver_text = _has_any(text, ("用户数", "客单价", "arppu", "aov", "单笔付费"))
    if has_driver_text:
        axes.append("driver_focus")
    if (
        _has_any(text, ("为什么", "原因", "上涨", "下跌", "变化", "增长", "下降"))
        and _has_any(
            text,
            (
                "付费金额",
                "收入",
                "首充",
                "付费频次",
                "单笔",
                "付费用户",
                "用户数",
                "客单价",
                "支付成功率",
            ),
        )
    ):
        axes.append("change_explanation")
    if _has_any(text, ("固定规律", "规律", "周末", "月初", "晚上")):
        axes.append("pattern_attribution")
    if _has_any(
        text,
        (
            "渠道",
            "一级渠道",
            "地区",
            "设备",
            "支付方式",
            "支付通道",
            "玩法",
            "用户类型",
            "wajespecial",
            "细分",
        ),
    ) and _has_any(
        text,
        ("哪些", "贡献", "贡献最大", "主要原因", "最明显", "带动", "拉动", "造成", "归因"),
    ):
        axes.append("factor_topk")
    if _has_any(
        text,
        ("活动", "投放预算", "素材", "版本更新", "支付通道", "节日", "外部事件"),
    ):
        axes.append("event_impact")
    if question_family == "revenue_health_review" or _has_any(
        text,
        (
            "收入健康",
            "正常用户增长",
            "少数大额",
            "短期活动",
            "异常渠道",
            "风险点",
            "新老用户",
            "新用户",
            "老用户",
            "用户质量",
            "用户类型",
            "高价值用户",
            "大额用户",
        ),
    ):
        axes.append("revenue_health")
    if _has_any(text, ("影响最大的", "3 个因子", "三个因子", "因子", "变化最大")):
        axes.append("factor_topk")
    if _has_any(text, ("异常波动", "异常", "偏离", "大额用户")) or (
        _has_any(text, ("移除", "剔除", "排除", "去掉", "排掉"))
        and _has_any(text, ("按日", "按天", "日期", "天", "日", "复算"))
    ):
        axes.append("anomaly")
    if _has_any(
        text,
        (
            "近 7 日",
            "近7日",
            "7 日均值",
            "7日均值",
            "上周同日",
            "前一天",
            "日均",
            "日平均",
            "按周",
            "周粒度",
            "口径改成",
            "换成",
        ),
    ):
        axes.append("multi_baseline")
    if question_family == "data_quality_or_evidence_review" or _has_any(
        text,
        ("证据够不够", "数据证据", "数据延迟", "归因异常", "支付状态", "重复订单"),
    ):
        axes.append("evidence_quality")
    if "joint_attribution" in requested and not has_driver_text:
        axes.append(
            "factor_topk"
            if _has_any(text, ("因子", "影响", "变化", "贡献", "带动", "拉动", "归因", "造成"))
            else "pattern_attribution"
        )
    if "rolling_window_compare" in requested:
        axes.append("multi_baseline")
    if "high_value_user_contribution" in requested or "user_mix_contribution" in requested:
        axes.append("revenue_health")
    if conversation_intent in {"new_topic", "correction", "clarification_answer"} and _has_any(
        text, ("付费金额", "收入")
    ):
        axes.append("change_explanation")
    if "factor_topk" in axes and "multi_baseline" not in axes:
        axes = [axis for axis in axes if axis != "change_explanation"]
    return _dedupe(axes)


def _revenue_diagnostic_nodes(axes: Iterable[str]) -> tuple[str, ...]:
    return _dedupe(
        node
        for axis in axes
        for node in REVENUE_DIAGNOSTIC_BUNDLES.get(axis, ())
    )


def _revenue_diagnostic_families(axes: Iterable[str]) -> tuple[str, ...]:
    return _dedupe(
        family
        for axis in axes
        for family in REVENUE_DIAGNOSTIC_FAMILIES.get(axis, ())
    )


def _families_from_nodes(nodes: Iterable[str]) -> tuple[str, ...]:
    families = []
    for node in nodes:
        if node in PUBLIC_CAPABILITIES:
            families.extend(get_capability_card(node).supported_question_families)
    return _dedupe(families)


def _order_capabilities(nodes: tuple[str, ...], diagnostic_axes: tuple[str, ...]) -> tuple[str, ...]:
    if not diagnostic_axes:
        return nodes
    indexed = list(enumerate(nodes))
    indexed.sort(key=lambda item: (CAPABILITY_ORDER.get(item[1], 500), item[0]))
    return tuple(node for _, node in indexed)


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token.lower() in text for token in tokens)


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
