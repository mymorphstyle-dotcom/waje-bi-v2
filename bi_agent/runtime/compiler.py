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
from bi_agent.runtime.revenue_runtime_plan import (
    build_revenue_runtime_plan,
    project_reviewed_contract_gaps,
)
from bi_agent.runtime.analysis_obligations import (
    ObligationRequest,
    resolve_partitioned_analysis_obligations,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


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
_CONVERSATION_DIAGNOSTIC_SUGGESTIONS = {
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
        "driver_decomposition",
        "candidate_dimension_screen",
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
CAPABILITY_ORDER = {
    "data_quality_check": 10,
    "data_quality_profile": 11,
    "compare_periods": 20,
    "rolling_window_compare": 21,
    "compare_period_phases": 22,
    "weekday_calendar_compare": 23,
    "pattern_scan": 30,
    "gameplay_activity_context": 31,
    "formula_decompose": 40,
    "driver_decomposition": 41,
    "candidate_dimension_screen": 49,
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
    bound_context: Optional[Mapping[str, Any]] = None,
    prior_analysis_assets: Iterable[Mapping[str, Any]] = (),
    registry: Optional[Mapping[str, RecipeEntry]] = None,
    runtime_registry: Optional[RuntimeContractRegistry] = None,
) -> CompiledGraph:
    registry = load_recipe_registry() if registry is None else registry
    runtime_registry = runtime_registry or RuntimeContractRegistry.from_path(
        CANONICAL_RUNTIME_BINDINGS_PATH
    )
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

    obligation_request = ObligationRequest.from_intent(
        question_family=question_family,
        question_families=tuple(question_families),
        target_metric=target_metric,
        bound_context=bound_context or {},
    )
    diagnostic_axes = obligation_request.diagnostic_tags
    try:
        obligation_resolution = resolve_partitioned_analysis_obligations(
            obligation_request, runtime_registry
        )
        diagnostic_axes = obligation_resolution.applicable_diagnostic_tags
        obligation_error = ""
    except ValueError as exc:
        obligation_resolution = None
        obligation_error = str(exc)
    obligation_nodes = (
        (
            *obligation_resolution.required_capabilities,
            *obligation_resolution.conditional_capabilities,
            *obligation_resolution.independent_capabilities,
        )
        if obligation_resolution is not None
        else ()
    )
    proposed_graph = _dedupe((*base_proposed_graph, *obligation_nodes))
    reviewed_gap_axes = _reviewed_gap_axes(
        question_family=question_family,
        question_families=tuple(question_families),
        accepted_graph=proposed_graph,
        diagnostic_axes=diagnostic_axes,
    )

    supported_families = frozenset(obligation_request.question_families)
    rejected_diagnostics = (
        obligation_resolution.rejected_diagnostic_tags
        if obligation_resolution is not None
        else ()
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
    if obligation_resolution is not None:
        records = (
            *records,
            *(
                MutationRecord(
                    action="rejected",
                    capability=str(mutation["capability"]),
                    reason=str(mutation["reason"]),
                )
                for mutation in obligation_resolution.mutations
                if mutation.get("action") == "rejected"
            ),
            *(
                MutationRecord(
                    action="auto_added",
                    capability=node,
                    reason=(
                        "obligation_conditional"
                        if node in obligation_resolution.conditional_capabilities
                        else "obligation_independent"
                        if node in obligation_resolution.independent_capabilities
                        else "obligation_required"
                    ),
                )
                for node in obligation_nodes
                if node not in base_proposed_graph
            ),
        )
    else:
        records = (
            *records,
            MutationRecord(
                action="rejected",
                capability=question_family,
                reason="obligation_conflict",
            ),
        )
    def make_runtime_plan(accepted: tuple[str, ...]) -> dict:
        return project_reviewed_contract_gaps(
            build_revenue_runtime_plan(
                target_metric=target_metric,
                accepted_graph=accepted,
                diagnostic_axes=diagnostic_axes,
                question_text="",
                bound_context=bound_context,
                prior_assets=prior_analysis_assets,
            ),
            reviewed_gap_axes,
        )

    if obligation_error:
        accepted = _order_capabilities(_dedupe(known_requested), diagnostic_axes)
        return _compiled(
            status="degraded",
            target_metric=target_metric,
            accepted=accepted,
            proposed=proposed_graph,
            rejected_or_degraded=_dedupe(
                (*unknown, *unsupported_for_family, *rejected_diagnostics, question_family)
            ),
            records=records,
            node_status="degraded",
            runtime_plan=make_runtime_plan(accepted),
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
            status=(
                "accepted"
                if not unknown and not unsupported_for_family and not rejected_diagnostics
                else "degraded"
            ),
            target_metric=target_metric,
            accepted=accepted,
            proposed=proposed_graph,
            rejected_or_degraded=_dedupe(
                (*unknown, *unsupported_for_family, *rejected_diagnostics)
            ),
            records=records,
            runtime_plan=make_runtime_plan(accepted),
        )

    if question_family == "custom_baseline_comparison" and known_requested:
        accepted = _order_capabilities(_dedupe(known_requested), diagnostic_axes)
        return _compiled(
            status=(
                "accepted"
                if not unknown and not unsupported_for_family and not rejected_diagnostics
                else "degraded"
            ),
            target_metric=target_metric,
            accepted=accepted,
            proposed=proposed_graph,
            rejected_or_degraded=_dedupe(
                (*unknown, *unsupported_for_family, *rejected_diagnostics)
            ),
            records=records,
            runtime_plan=make_runtime_plan(accepted),
        )

    if explicit_requested and known_requested:
        accepted = _order_capabilities(_dedupe(known_requested), diagnostic_axes)
        return _compiled(
            status=(
                "accepted"
                if not unknown and not unsupported_for_family and not rejected_diagnostics
                else "degraded"
            ),
            target_metric=target_metric,
            accepted=accepted,
            proposed=proposed_graph,
            rejected_or_degraded=_dedupe(
                (*unknown, *unsupported_for_family, *rejected_diagnostics)
            ),
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
            (
                *unknown,
                *unsupported_for_family,
                *rejected_diagnostics,
                *accepted,
                *skipped_supported,
            )
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
    plan = runtime_plan or {}
    analysis_contract = plan.get("analysis_contract")
    analysis_contract = (
        dict(analysis_contract) if isinstance(analysis_contract, Mapping) else {}
    )
    query_contracts = tuple(
        dict(item)
        for item in plan.get("query_contracts") or ()
        if isinstance(item, Mapping)
    )
    capability_execution_plans = tuple(
        dict(item)
        for item in plan.get("capability_execution_plans") or ()
        if isinstance(item, Mapping)
    )
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
        runtime_plan=plan,
        analysis_contract=analysis_contract,
        query_contracts=query_contracts,
        capability_execution_plans=capability_execution_plans,
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
        for node in _CONVERSATION_DIAGNOSTIC_SUGGESTIONS.get(axis, ())
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


def _reviewed_gap_axes(
    *,
    question_family: str,
    question_families: tuple[str, ...],
    accepted_graph: tuple[str, ...],
    diagnostic_axes: tuple[str, ...],
) -> tuple[str, ...]:
    families = set((question_family, *question_families))
    axes = list(diagnostic_axes)
    if "data_quality_or_evidence_review" in families:
        axes.append("evidence_quality")
    if "event_evidence" in accepted_graph or "event_window_compare" in accepted_graph:
        axes.append("event_impact")
    return _dedupe(axes)


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token.lower() in text for token in tokens)


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
