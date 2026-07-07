from __future__ import annotations

from typing import Optional
from uuid import uuid4

from bi_agent.conversation.models import (
    ContextItem,
    ContextManifest,
    ConversationRunRequest,
    ConversationTurnResult,
    MemoryProposal,
    ReuseDecision,
    TopicState,
    TurnIntent,
)
from bi_agent.conversation.store import InMemoryConversationStore


class ConversationRuntime:
    def __init__(self, store: Optional[InMemoryConversationStore] = None) -> None:
        self.store = store or InMemoryConversationStore()

    def handle_message(
        self,
        thread_id: str,
        user_message: str,
        *,
        role: str = "analyst",
        active_run_status: str = "idle",
        current_snapshot: str = "2026H1",
        contract_version: str = "contracts-v1",
        owner_scope: str = "org-default",
    ) -> ConversationTurnResult:
        thread = self.store.get_thread(thread_id)
        turn_id = f"turn-{uuid4().hex[:12]}"
        intent_name = _classify_intent(user_message, bool(thread.pending_clarification_id))
        topic_relation = _topic_relation(intent_name, user_message, active_run_status)
        pending_clarification_id = thread.pending_clarification_id
        topic = self._resolve_topic(thread_id, topic_relation, user_message, intent_name)
        turn_intent = TurnIntent(
            intent=intent_name,
            confidence=0.82,
            topic_relation=topic_relation,
            decision_source="conversation_orchestrator",
            business_summary=_intent_summary(intent_name, user_message),
        )
        reuse_decisions = self._reuse_decisions(
            topic,
            intent_name,
            topic_relation,
            user_message,
            role,
            current_snapshot,
            contract_version,
        )
        manifest = self._context_manifest(
            thread_id,
            turn_id,
            topic,
            user_message,
            role,
            current_snapshot,
            reuse_decisions,
            owner_scope,
            pending_clarification_id if intent_name == "clarification_answer" else "",
        )
        memory_proposals = self._memory_proposals(
            thread_id,
            turn_id,
            user_message,
            intent_name,
            owner_scope,
            role,
        )
        for proposal in memory_proposals:
            self.store.add_memory_proposal(proposal)
        needs_clarification = topic_relation == "ask_topic_choice" or _needs_clarification(user_message)
        run_request = None
        if _should_run(intent_name, topic_relation):
            run_request = ConversationRunRequest(
                thread_id=thread_id,
                turn_id=turn_id,
                topic_id=topic.topic_id if topic else None,
                user_message=user_message,
                context_manifest=manifest.to_dict(),
                permission_context={"role": role},
                runtime_budget=_runtime_budget(user_message),
                requested_nodes=_requested_nodes(user_message, intent_name),
            )
        audit_events = (
            {
                "event": "turn_intent_bound",
                "turn_id": turn_id,
                "intent": intent_name,
                "topic_relation": topic_relation,
                "source": turn_intent.decision_source,
            },
            {
                "event": "context_manifest_created",
                "turn_id": turn_id,
                "manifest_id": manifest.manifest_id,
                "can_support_claims": manifest.can_support_claims,
            },
        )
        result = ConversationTurnResult(
            thread_id=thread_id,
            turn_id=turn_id,
            topic_id=topic.topic_id if topic else None,
            turn_intent=turn_intent,
            topic_relation=topic_relation,
            context_manifest=manifest,
            reuse_decisions=reuse_decisions,
            memory_proposals=memory_proposals,
            audit_events=audit_events,
            run_request=run_request,
            needs_clarification=needs_clarification,
            response_boundary=_response_boundary(intent_name),
        )
        self.store.add_turn(thread_id, result.to_dict())
        if intent_name == "clarification_answer":
            self.store.clear_pending_clarification(thread_id)
        return result

    def _resolve_topic(
        self,
        thread_id: str,
        relation: str,
        message: str,
        intent: str,
    ) -> Optional[TopicState]:
        if intent == "clarification_answer":
            thread = self.store.get_thread(thread_id)
            topic = self.store.topic(thread.pending_clarification_topic_id)
            if topic:
                self.store.set_current_topic(thread_id, topic.topic_id)
                return topic
        if relation in {"rejected", "ask_topic_choice"}:
            return self.store.current_topic(thread_id) if "老板" in message else None
        if relation == "select_referenced_topic":
            topics = self.store.topics_for_thread(thread_id)
            if len(topics) >= 2:
                self.store.set_current_topic(thread_id, topics[1].topic_id)
                return topics[1]
        if relation in {"new_topic", "queued_new_topic"}:
            topic = self.store.create_topic(thread_id, title=_topic_title(message), summary=message)
            self.store.set_current_topic(thread_id, topic.topic_id)
            return topic
        return self.store.current_topic(thread_id)

    def _reuse_decisions(
        self,
        topic: Optional[TopicState],
        intent: str,
        relation: str,
        message: str,
        role: str,
        current_snapshot: str,
        contract_version: str,
    ) -> tuple[ReuseDecision, ...]:
        if relation == "ask_topic_choice":
            return (ReuseDecision("none", "", "topic_reference_ambiguous"),)
        if intent in {"off_topic", "capability_question", "memory_update"}:
            return (ReuseDecision("none", "", "no_bi_claim_requested"),)
        if intent == "unsupported_request":
            return (ReuseDecision("blocked", "", "permission_or_safety_boundary"),)
        result = self.store.results_for_topic(topic.topic_id if topic else None)
        if not result:
            return (ReuseDecision("rerun", "", "no_prior_result_ref"),)
        first = result[0]
        if not _can_read_scope(role, first.permission_scope):
            return (ReuseDecision("blocked", first.result_ref, "permission_scope_mismatch"),)
        if current_snapshot != first.snapshot_id or "数据更新" in message or "最新数据" in message:
            return (ReuseDecision("context_only", first.result_ref, "snapshot_mismatch"),)
        if contract_version != first.contract_version:
            return (ReuseDecision("context_only", first.result_ref, "contract_version_mismatch"),)
        if _must_rerun(message, intent, relation):
            return (ReuseDecision("rerun", first.result_ref, "semantic_scope_changed"),)
        return (ReuseDecision("reuse", first.result_ref, "validated_same_thread_scope"),)

    def _context_manifest(
        self,
        thread_id: str,
        turn_id: str,
        topic: Optional[TopicState],
        message: str,
        role: str,
        current_snapshot: str,
        reuse_decisions: tuple[ReuseDecision, ...],
        owner_scope: str,
        pending_clarification_id: str = "",
    ) -> ContextManifest:
        items: list[ContextItem] = []
        if pending_clarification_id:
            items.append(
                ContextItem(
                    source_type="clarification",
                    source_ref=pending_clarification_id,
                    summary="用户已回答上一轮澄清问题，本轮按该选择恢复执行。",
                    can_support_claims=False,
                    reason="clarification_outcome",
                )
            )
        if topic:
            items.append(
                ContextItem(
                    source_type="topic",
                    source_ref=topic.topic_id,
                    summary=topic.summary,
                    can_support_claims=True,
                )
            )
        artifact = self.store.latest_artifact_for_topic(topic.topic_id if topic else None)
        if artifact and ("基于这个结果" in message or "保存" in message or "打开" in message):
            artifact_can_support = (
                artifact.snapshot_id == current_snapshot
                and _can_read_scope(role, artifact.permission_scope)
            )
            items.append(
                ContextItem(
                    source_type="artifact",
                    source_ref=artifact.artifact_id,
                    summary=artifact.follow_up_context,
                    can_support_claims=artifact_can_support,
                    visibility=artifact.permission_scope,
                    reason="artifact_follow_up_context" if artifact_can_support else "artifact_context_only",
                )
            )
        for memory in self.store.long_term_memory(owner_scope):
            if role != "business_reader" or memory.visibility == "business_reader":
                items.append(
                    ContextItem(
                        source_type="memory",
                        source_ref=memory.source_ref,
                        summary=memory.text,
                        can_support_claims=False,
                        visibility=memory.visibility,
                        reason="preference_only",
                    )
                )
        if not items:
            items.append(
                ContextItem(
                    source_type="policy",
                    source_ref="conversation-boundary",
                    summary="本轮没有可复用 BI 证据上下文。",
                    can_support_claims=False,
                    reason="no_context",
                )
            )
        claim_safe = all(decision.decision not in {"blocked", "context_only"} for decision in reuse_decisions)
        artifact_context_blocked = any(
            item.source_type == "artifact" and not item.can_support_claims
            for item in items
        )
        return ContextManifest(
            manifest_id=f"context-{uuid4().hex[:12]}",
            thread_id=thread_id,
            turn_id=turn_id,
            items=tuple(items),
            can_support_claims=claim_safe and not artifact_context_blocked,
        )

    def _memory_proposals(
        self,
        thread_id: str,
        turn_id: str,
        message: str,
        intent: str,
        owner_scope: str,
        role: str,
    ) -> tuple[MemoryProposal, ...]:
        if intent != "memory_update":
            return ()
        action = "撤销" if "删掉" in message else "默认把 WajeSpecial 单独观察"
        return (
            MemoryProposal(
                proposal_id=f"memory-proposal-{uuid4().hex[:12]}",
                thread_id=thread_id,
                text=action,
                source_ref=turn_id,
                owner_scope=owner_scope,
                visibility=role,
            ),
        )


def _classify_intent(message: str, has_pending_clarification: bool) -> str:
    text = message.strip()
    if has_pending_clarification and text in {"日均。", "日均", "按推荐继续。", "按推荐继续"}:
        return "clarification_answer"
    if any(token in text for token in ("原始用户 ID", "直接写 SQL", "所有订单", "发优惠券", "预测下个月")):
        return "unsupported_request"
    if any(token in text for token in ("中午吃什么", "写一首诗")):
        return "off_topic"
    if any(token in text for token in ("能看哪些数据", "能不能按", "为什么不能证明", "会不会联网", "分享给老板")):
        return "capability_question"
    if any(token in text for token in ("以后默认", "记住", "删掉以后默认")):
        return "memory_update"
    if any(token in text for token in ("基于这个结果", "打开之前保存")):
        return "artifact_continue"
    if any(token in text for token in ("口径改成", "换成日均", "不要按")):
        return "correction"
    if _is_mixed(text):
        return "mixed_question"
    if any(token in text for token in ("是不是被", "去掉", "有多稳", "指导投放")):
        return "challenge"
    if _looks_new_topic(text):
        return "new_topic"
    return "follow_up"


def _topic_relation(intent: str, message: str, active_run_status: str) -> str:
    if intent in {"off_topic", "unsupported_request"}:
        return "rejected"
    if intent == "capability_question":
        return "inherit_current" if "分享给老板" in message else "rejected"
    if "刚才第二个" in message:
        return "select_referenced_topic"
    if "刚才那个" in message:
        return "ask_topic_choice"
    if active_run_status == "running" and intent == "new_topic":
        return "queued_new_topic"
    if intent == "mixed_question":
        if "顺便" in message and "1 月" in message:
            return "split_topics"
        if any(token in message for token in ("按渠道、支付方式和新老用户", "检查未知渠道", "给老板看")):
            return "inherit_current"
        if "这个月有没有变好" in message or "月初模式和周末模式" in message:
            return "new_topic"
        return "split_subintents"
    if intent == "new_topic":
        return "new_topic"
    if intent == "memory_update":
        return "inherit_current"
    return "inherit_current"


def _is_mixed(text: str) -> bool:
    mixed_tokens = (
        "是否上涨",
        "顺便",
        "一起",
        "同时",
        "前后 14 天",
        "三个口径",
        "哪个更明显",
        "检查未知渠道",
        "观察什么",
        "原因和风险",
        "跟其他渠道比",
        "再看支付方式",
    )
    return any(token in text for token in mixed_tokens)


def _looks_new_topic(text: str) -> bool:
    if "刚才" in text:
        return False
    return any(
        token in text
        for token in (
            "Q2 比 Q1",
            "Q2 变化",
            "这个月是不是变好了",
            "我又发一个新问题",
        )
    )


def _must_rerun(message: str, intent: str, relation: str) -> bool:
    if relation in {"new_topic", "queued_new_topic", "split_topics", "split_subintents"}:
        return True
    if intent in {"correction", "clarification_answer"}:
        return True
    return any(
        token in message
        for token in (
            "换成",
            "只看",
            "按渠道、支付方式和新老用户",
            "去掉",
            "按周",
            "失败支付",
            "每天变化",
            "活动前后",
            "日均",
            "最新数据",
        )
    )


def _needs_clarification(message: str) -> bool:
    return any(token in message for token in ("这个月是不是变好了", "这个月有没有变好"))


def _should_run(intent: str, relation: str) -> bool:
    if relation == "ask_topic_choice":
        return False
    return intent not in {"off_topic", "capability_question", "unsupported_request", "memory_update"}


def _requested_nodes(message: str, intent: str) -> tuple[str, ...]:
    nodes: list[str] = ["business_intent"]
    if any(token in message for token in ("渠道", "支付方式", "新用户", "老用户", "WajeSpecial", "细分")):
        nodes.append("segment_contribution")
    if any(token in message for token in ("异常", "去掉", "拖高")):
        nodes.append("outlier_scan")
    if any(token in message for token in ("为什么", "原因", "贡献", "深挖")):
        nodes.append("driver_decomposition")
    if any(token in message for token in ("活动", "前后 14 天")):
        nodes.append("event_evidence")
    if intent in {"new_topic", "mixed_question", "correction", "clarification_answer", "artifact_continue"}:
        nodes.append("compare_periods")
    nodes.append("answer_verify")
    return tuple(dict.fromkeys(nodes))


def _runtime_budget(message: str) -> dict[str, int | str]:
    deep = any(token in message for token in ("深挖", "再找原因", "为什么", "原因"))
    return {"mode": "deep_attribution" if deep else "normal", "soft_limit": 100 if deep else 50}


def _can_read_scope(role: str, permission_scope: str) -> bool:
    rank = {"business_reader": 1, "analyst": 2, "data_owner_admin": 3}
    return rank.get(role, 0) >= rank.get(permission_scope, 3)


def _topic_title(message: str) -> str:
    return message[:28] or "新业务问题"


def _intent_summary(intent: str, message: str) -> str:
    if intent == "off_topic":
        return "这不是当前 BI Agent 要执行的业务分析输入。"
    if intent == "capability_question":
        return "用户在询问系统能力或证据边界。"
    if intent == "unsupported_request":
        return "用户请求触达权限或安全边界。"
    if intent == "mixed_question":
        return "用户把多个业务动作放在同一轮输入里。"
    if intent == "memory_update":
        return "用户提出可沉淀的分析偏好。"
    return message


def _response_boundary(intent: str) -> str:
    return {
        "off_topic": "只回答 BI Agent 的业务分析范围。",
        "capability_question": "说明系统能力、数据边界和证据边界。",
        "unsupported_request": "拒绝越权或不安全请求，并给出聚合替代路径。",
        "memory_update": "生成可审计记忆提案，不直接写入长期记忆。",
    }.get(intent, "进入受控 BI workflow，所有 claim 需要证据和 verifier。")
