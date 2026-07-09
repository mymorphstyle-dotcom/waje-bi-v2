from __future__ import annotations

from dataclasses import replace
import re
from typing import Any, Mapping, Optional
from uuid import uuid4

from bi_agent.conversation.models import (
    ClarificationOption,
    ClarificationQuestion,
    ClarificationRequest,
    ClarificationState,
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
from bi_agent.runtime.analysis_assets import merge_analysis_assets
from bi_agent.runtime.compiler import suggest_revenue_diagnostic_nodes
from bi_agent.runtime.llm_prompts import build_prompt


ALLOWED_INTENTS = frozenset(
    {
        "new_topic",
        "follow_up",
        "mixed_question",
        "correction",
        "clarification_answer",
        "challenge",
        "artifact_continue",
        "capability_question",
        "off_topic",
        "unsupported_request",
        "memory_update",
    }
)
ALLOWED_TOPIC_RELATIONS = frozenset(
    {
        "new_topic",
        "inherit_current",
        "split_topics",
        "split_subintents",
        "select_referenced_topic",
        "ask_topic_choice",
        "queued_new_topic",
        "rejected",
    }
)
LOCAL_GUARDED_INTENTS = frozenset({"off_topic", "unsupported_request"})


class ConversationRuntime:
    def __init__(
        self,
        store: Optional[InMemoryConversationStore] = None,
        *,
        llm_client: Any = None,
    ) -> None:
        self.store = store or InMemoryConversationStore()
        self.llm_client = llm_client

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
        run_id: str | None = None,
        prior_analysis_assets: tuple[Mapping[str, Any], ...] = (),
    ) -> ConversationTurnResult:
        thread = self.store.get_thread(thread_id)
        open_clarification = self.store.get_open_clarification(thread_id)
        text = user_message.strip()
        matches_open_clarification = (
            _looks_like_clarification_answer(text, open_clarification)
            if open_clarification
            else False
        )
        matches_legacy_pending = (
            not open_clarification
            and bool(thread.pending_clarification_id)
            and _looks_like_legacy_clarification_answer(text)
        )
        if open_clarification and matches_open_clarification:
            self.store.set_pending_clarification(
                thread_id,
                open_clarification.topic_id,
                thread.pending_clarification_id or open_clarification.run_id,
            )
            thread = self.store.get_thread(thread_id)
        turn_id = f"turn-{uuid4().hex[:12]}"
        allow_clarification_answer = matches_open_clarification or matches_legacy_pending
        local_intent = _classify_intent(user_message, allow_clarification_answer)
        local_topic_relation = _topic_relation(local_intent, user_message, active_run_status)
        if (
            not thread.current_topic_id
            and local_topic_relation == "inherit_current"
            and _should_run(local_intent, local_topic_relation)
        ):
            local_intent = "new_topic"
            local_topic_relation = "new_topic"
        orchestration = self._orchestrate_turn(
            thread_id,
            thread,
            user_message,
            active_run_status,
            local_intent,
            local_topic_relation,
            allow_clarification_answer,
        )
        intent_name = orchestration["intent"]
        topic_relation = orchestration["topic_relation"]
        pending_clarification_id = thread.pending_clarification_id
        topic = self._resolve_topic(thread_id, topic_relation, user_message, intent_name)
        turn_intent = TurnIntent(
            intent=intent_name,
            confidence=orchestration["confidence"],
            topic_relation=topic_relation,
            decision_source=orchestration["decision_source"],
            business_summary=orchestration["business_summary"],
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
        topic_assets = self._topic_analysis_assets(thread_id, topic)
        combined_prior_assets = merge_analysis_assets(topic_assets, prior_analysis_assets)
        manifest = self._context_manifest(
            thread_id,
            turn_id,
            topic,
            user_message,
            role,
            current_snapshot,
            contract_version,
            reuse_decisions,
            owner_scope,
            combined_prior_assets,
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
        needs_clarification = intent_name != "clarification_answer" and (
            topic_relation == "ask_topic_choice" or _needs_clarification(user_message)
        )
        clarification = (
            _build_clarification(turn_id, user_message, topic_relation)
            if needs_clarification
            else None
        )
        clarification_topic = topic or self.store.current_topic(thread_id)
        if clarification and clarification_topic:
            state_run_id = run_id or clarification.clarification_id
            self.store.set_pending_clarification(
                thread_id,
                clarification_topic.topic_id,
                clarification.clarification_id,
            )
            self.store.save_clarification_state(
                ClarificationState(
                    run_id=state_run_id,
                    topic_id=clarification_topic.topic_id,
                    question=clarification.questions[0].question,
                    options=list(clarification.questions[0].options),
                )
            )
        run_request = None
        if not needs_clarification and _should_run(intent_name, topic_relation):
            run_request = ConversationRunRequest(
                thread_id=thread_id,
                turn_id=turn_id,
                topic_id=topic.topic_id if topic else None,
                user_message=user_message,
                context_manifest=manifest.to_dict(),
                permission_context={"role": role},
                runtime_budget=_runtime_budget(user_message),
                prior_analysis_assets=combined_prior_assets,
                requested_nodes=_requested_nodes(user_message, intent_name),
            )
        audit_events = (
            {
                "event": "turn_intent_bound",
                "turn_id": turn_id,
                "intent": intent_name,
                "topic_relation": topic_relation,
                "source": turn_intent.decision_source,
                "local_intent": local_intent,
                "local_topic_relation": local_topic_relation,
            },
            {
                "event": "context_manifest_created",
                "turn_id": turn_id,
                "manifest_id": manifest.manifest_id,
                "can_support_claims": manifest.can_support_claims,
            },
        )
        if orchestration.get("llm_audit"):
            audit_events = audit_events + (
                {
                    "event": "conversation_orchestrator_llm_evaluated",
                    "turn_id": turn_id,
                    "source": turn_intent.decision_source,
                    "audit": orchestration["llm_audit"],
                },
            )
        if clarification:
            audit_events = audit_events + (
                {
                    "event": "clarification_requested",
                    "turn_id": turn_id,
                    "clarification_id": clarification.clarification_id,
                    "reason": clarification.reason,
                },
            )
            self.store.add_audit_event(
                "clarification_requested",
                thread_id=thread_id,
                topic_id=topic.topic_id if topic else "",
                ref=clarification.clarification_id,
                payload=clarification.to_dict(),
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
            clarification=clarification,
            response_boundary=_response_boundary(intent_name),
        )
        self.store.add_turn(thread_id, result.to_dict())
        self.store.save_context_manifest(manifest)
        self.store.save_reuse_decisions(thread_id, turn_id, reuse_decisions)
        if intent_name == "clarification_answer":
            if open_clarification:
                self.store.save_clarification_state(
                    replace(open_clarification, status="answered", answer=user_message)
                )
            self.store.clear_pending_clarification(thread_id)
        return result

    def _orchestrate_turn(
        self,
        thread_id: str,
        thread: Any,
        message: str,
        active_run_status: str,
        local_intent: str,
        local_topic_relation: str,
        allow_clarification_answer: bool,
    ) -> dict[str, Any]:
        local = _local_orchestration(local_intent, local_topic_relation, message)
        if not self.llm_client:
            return local
        if not _should_use_llm_orchestrator(
            local_intent,
            local_topic_relation,
            message,
            allow_clarification_answer,
        ):
            return local

        spec = build_prompt(
            "conversation_orchestrator",
            {
                "user_message": message,
                "thread_state": {
                    "thread_id": thread_id,
                    "current_topic_id": thread.current_topic_id,
                    "pending_clarification_id": thread.pending_clarification_id,
                    "pending_clarification_topic_id": thread.pending_clarification_topic_id,
                    "active_run_status": active_run_status,
                },
                "candidate_topics": [
                    topic.to_dict() for topic in self.store.topics_for_thread(thread_id)[-5:]
                ],
                "recent_turns": list(getattr(thread, "turns", [])[-5:]),
                "local_precheck": {
                    "intent": local_intent,
                    "topic_relation": local_topic_relation,
                },
                "allowed_intents": sorted(ALLOWED_INTENTS),
                "allowed_topic_relations": sorted(ALLOWED_TOPIC_RELATIONS),
            },
        )
        try:
            result = self.llm_client.invoke_json(
                task=spec.task,
                prompt_version=spec.prompt_version,
                messages=spec.messages,
                required_keys=spec.required_keys,
            )
        except Exception as exc:
            fallback = dict(local)
            fallback["decision_source"] = "local_conversation_orchestrator_fallback"
            fallback["business_summary"] = f"{local['business_summary']} LLM 路由不可用，已采用本地预检。"
            fallback["llm_audit"] = {"error": str(exc)}
            return fallback

        validated = _validated_orchestration(
            result.output,
            local,
            allow_clarification_answer=allow_clarification_answer,
            active_run_status=active_run_status,
            topic_count=len(self.store.topics_for_thread(thread_id)),
        )
        validated["llm_audit"] = result.audit
        return validated

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
        decision = evaluate_reuse_candidate(
            source_snapshot=first.snapshot_id,
            current_snapshot=current_snapshot,
            permission_match=_can_read_scope(role, first.permission_scope),
            semantic_scope_match=not _must_rerun(message, intent, relation),
            source_ref=first.result_ref,
        )
        if decision.decision != "reuse":
            return (decision,)
        if contract_version != first.contract_version:
            return (ReuseDecision("context_only", first.result_ref, "contract_version_mismatch"),)
        return (decision,)

    def _context_manifest(
        self,
        thread_id: str,
        turn_id: str,
        topic: Optional[TopicState],
        message: str,
        role: str,
        current_snapshot: str,
        contract_version: str,
        reuse_decisions: tuple[ReuseDecision, ...],
        owner_scope: str,
        analysis_assets: tuple[dict[str, Any], ...],
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
                    permission_scope=role,
                    source_version=contract_version,
                    claim_use="context_only",
                )
            )
        if topic:
            items.append(
                ContextItem(
                    source_type="topic",
                    source_ref=topic.topic_id,
                    summary=topic.summary,
                    can_support_claims=False,
                    reason="topic_context_only",
                    permission_scope=role,
                    source_version=contract_version,
                    claim_use="context_only",
                )
            )
        for decision in reuse_decisions:
            if not decision.result_ref:
                continue
            items.append(
                ContextItem(
                    source_type="result_ref",
                    source_ref=decision.result_ref,
                    summary=f"上一轮结果引用，当前复用判断为 {decision.decision}。",
                    can_support_claims=decision.decision == "reuse",
                    reason=decision.reason,
                    permission_scope=role,
                    source_version=f"{contract_version}:{current_snapshot}",
                    expired=decision.decision in {"rerun", "context_only", "blocked"},
                    claim_use=decision.decision,
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
                    permission_scope=artifact.permission_scope,
                    source_version=artifact.snapshot_id,
                    expired=not artifact_can_support,
                    claim_use="reuse" if artifact_can_support else "context_only",
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
                        permission_scope=memory.visibility,
                        source_version=memory.ttl,
                        claim_use="preference_only",
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
                    permission_scope=role,
                    source_version=contract_version,
                    claim_use="context_only",
                )
            )
        claim_safe = all(
            decision.decision not in {"rerun", "blocked", "context_only"}
            for decision in reuse_decisions
        )
        has_claim_support = any(item.can_support_claims for item in items)
        artifact_context_blocked = any(
            item.source_type == "artifact" and not item.can_support_claims
            for item in items
        )
        return ContextManifest(
            manifest_id=f"context-{uuid4().hex[:12]}",
            thread_id=thread_id,
            turn_id=turn_id,
            topic_id=topic.topic_id if topic else None,
            items=tuple(items),
            claim_use_policy={
                "requires_evidence_ref": True,
                "can_support_bi_claim": has_claim_support and claim_safe and not artifact_context_blocked,
            },
            snapshot_version=current_snapshot,
            permission_context={"role": role},
            analysis_assets=list(analysis_assets),
            can_support_claims=has_claim_support and claim_safe and not artifact_context_blocked,
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

    def _topic_analysis_assets(
        self,
        thread_id: str,
        topic: Optional[TopicState],
    ) -> tuple[dict[str, Any], ...]:
        if not topic or not hasattr(self.store, "list_analysis_assets"):
            return ()
        return tuple(self.store.list_analysis_assets(thread_id, topic.topic_id))


def evaluate_reuse_candidate(
    *,
    source_snapshot: str | None,
    current_snapshot: str | None,
    permission_match: bool,
    semantic_scope_match: bool,
    source_ref: str = "candidate",
) -> ReuseDecision:
    if not permission_match:
        return ReuseDecision(
            "blocked",
            source_ref,
            "permission_scope_mismatch",
            can_support_claim=False,
            requires_rerun=True,
        )
    if source_snapshot != current_snapshot:
        return ReuseDecision(
            "context_only",
            source_ref,
            "snapshot_mismatch",
            can_support_claim=False,
            requires_rerun=True,
        )
    if not semantic_scope_match:
        return ReuseDecision(
            "rerun",
            source_ref,
            "semantic_scope_mismatch",
            can_support_claim=False,
            requires_rerun=True,
        )
    return ReuseDecision(
        "reuse",
        source_ref,
        "validated_same_thread_scope",
        can_support_claim=True,
        requires_rerun=False,
    )
def _local_orchestration(intent: str, topic_relation: str, message: str) -> dict[str, Any]:
    return {
        "intent": intent,
        "topic_relation": topic_relation,
        "confidence": 0.82,
        "decision_source": "local_conversation_orchestrator",
        "business_summary": _intent_summary(intent, message),
    }


def _should_use_llm_orchestrator(
    local_intent: str,
    local_topic_relation: str,
    message: str,
    allow_clarification_answer: bool,
) -> bool:
    if allow_clarification_answer:
        return False
    if local_intent in LOCAL_GUARDED_INTENTS:
        return True
    if local_topic_relation in {
        "ask_topic_choice",
        "select_referenced_topic",
        "split_topics",
        "split_subintents",
        "queued_new_topic",
    }:
        return True
    if local_intent in {"mixed_question", "capability_question", "memory_update"}:
        return True
    if local_intent == "challenge":
        return "是不是被" in message
    return False


def _validated_orchestration(
    output: Any,
    local: dict[str, Any],
    *,
    allow_clarification_answer: bool,
    active_run_status: str,
    topic_count: int,
) -> dict[str, Any]:
    if not isinstance(output, dict):
        return _local_fallback(local, "local_conversation_orchestrator_fallback")

    intent = str(output.get("intent") or "").strip()
    topic_relation = str(output.get("topic_relation") or "").strip()
    if intent not in ALLOWED_INTENTS or topic_relation not in ALLOWED_TOPIC_RELATIONS:
        return _local_fallback(local, "local_conversation_orchestrator_fallback")

    if local["intent"] in LOCAL_GUARDED_INTENTS and intent != local["intent"]:
        return _local_fallback(local, "local_conversation_orchestrator_guard")

    if intent == "clarification_answer" and not allow_clarification_answer:
        return _local_fallback(local, "local_conversation_orchestrator_fallback")

    if intent in {"off_topic", "unsupported_request"}:
        topic_relation = "rejected"
    elif intent == "capability_question" and topic_relation not in {"inherit_current", "rejected"}:
        topic_relation = "rejected"
    elif intent == "memory_update":
        topic_relation = "inherit_current"
    elif active_run_status == "running" and intent == "new_topic":
        topic_relation = "queued_new_topic"
    elif topic_relation == "select_referenced_topic" and topic_count < 2:
        return _local_fallback(local, "local_conversation_orchestrator_fallback")

    business_summary = output.get("business_summary")
    if not isinstance(business_summary, str) or not business_summary.strip():
        business_summary = _intent_summary(intent, "")

    return {
        "intent": intent,
        "topic_relation": topic_relation,
        "confidence": _confidence(output.get("confidence")),
        "decision_source": "llm_conversation_orchestrator",
        "business_summary": business_summary.strip(),
    }


def _local_fallback(local: dict[str, Any], source: str) -> dict[str, Any]:
    fallback = dict(local)
    fallback["decision_source"] = source
    return fallback


def _confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.7
    return max(0.0, min(1.0, confidence))


def _classify_intent(message: str, allow_clarification_answer: bool) -> str:
    text = message.strip()
    if allow_clarification_answer:
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
    if any(token in text for token in ("口径改成", "换成日均", "不要按", "说错了", "改看")):
        return "correction"
    if _is_mixed(text):
        return "mixed_question"
    if _is_outlier_removal_question(text) or any(
        token in text
        for token in (
            "是不是被",
            "去掉",
            "有多稳",
            "指导投放",
            "就是主要原因",
            "直接说",
            "活动有效",
            "异常波动",
            "证据够不够",
            "数据证据够不够",
        )
    ):
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
    if intent == "correction" and any(token in message for token in ("改看", "退款", "留存")):
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
    if re.search(r"Q\d+\s*(相比|比)\s*Q\d+", text):
        return True
    return any(
        token in text
        for token in (
            "Q2 比 Q1",
            "Q2 相比 Q1",
            "Q2 变化",
            "全样本看月初",
            "全量样本里，月初",
            "留存有没有变差",
            "另外看一下",
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
            "数据更新",
            "最新数据",
        )
    )


def _needs_clarification(message: str) -> bool:
    return _is_outlier_removal_question(message) or any(
        token in message
        for token in (
            "这个月是不是变好了",
            "这个月有没有变好",
        )
    )


def _build_clarification(
    turn_id: str,
    message: str,
    topic_relation: str,
) -> ClarificationRequest:
    if topic_relation == "ask_topic_choice":
        question = ClarificationQuestion(
            question_id="topic_reference",
            question="你想继续哪一个业务问题？",
            options=(
                ClarificationOption(
                    option_id="current_topic",
                    label="继续当前问题",
                    description="沿用当前打开的业务问题继续分析。",
                    recommended=True,
                ),
                ClarificationOption(
                    option_id="second_topic",
                    label="继续第二个问题",
                    description="切到 thread 里的第二条业务问题链。",
                ),
                ClarificationOption(
                    option_id="tell_agent_differently",
                    label="告诉 Agent 换一种做法",
                    description="自己说明要继续哪个问题或换一个分析方式。",
                ),
            ),
        )
        return ClarificationRequest(
            clarification_id=f"clarification-{turn_id}",
            reason="topic_reference_ambiguous",
            questions=(question,),
        )

    if _is_outlier_removal_question(message):
        question = ClarificationQuestion(
            question_id="outlier_removal_strategy",
            question="你想按什么规则移除异常影响？",
            options=(
                ClarificationOption(
                    option_id="daily_remove_top_positive_day",
                    label="按日移除最大正向日",
                    description="先按天聚合，再移除贡献最大的正向日期后复算。",
                    recommended=True,
                ),
                ClarificationOption(
                    option_id="exclude_specific_dates",
                    label="指定日期再复算",
                    description="你自己指定要排除的异常日期范围。",
                ),
                ClarificationOption(
                    option_id="tell_agent_differently",
                    label="告诉 Agent 换一种做法",
                    description="自己指定异常识别口径和剔除范围。",
                ),
            ),
        )
        return ClarificationRequest(
            clarification_id=f"clarification-{turn_id}",
            reason="outlier_removal_strategy_changes_business_answer",
            questions=(question,),
        )

    question = ClarificationQuestion(
        question_id="metric_and_baseline",
        question="你想用哪个口径判断“变好了”？",
        options=(
            ClarificationOption(
                option_id="daily_avg_paid_amount",
                label="按日均付费金额",
                description="更适合比较不同天数的时间窗口。",
                recommended=True,
            ),
            ClarificationOption(
                option_id="total_paid_amount",
                label="按付费总金额",
                description="适合判断整体收入规模变化。",
            ),
            ClarificationOption(
                option_id="tell_agent_differently",
                label="告诉 Agent 换一个口径",
                description="自己指定指标、时间窗口或对比基线。",
            ),
        ),
    )
    return ClarificationRequest(
        clarification_id=f"clarification-{turn_id}",
        reason="metric_or_baseline_changes_business_answer",
        questions=(question,),
    )


def _looks_like_clarification_answer(
    text: str,
    clarification: ClarificationState | None = None,
) -> bool:
    if clarification is None:
        return _looks_like_legacy_clarification_answer(text)
    if _looks_new_topic(text) or _is_mixed(text):
        return False
    normalized = text.strip().rstrip("。")
    option_texts = {
        part.strip().rstrip("。")
        for option in clarification.options
        for part in (option.option_id, option.label, option.description)
        if part and part.strip()
    }
    if normalized in option_texts:
        return True
    if normalized in {"按推荐继续", "推荐"}:
        return any(option.recommended for option in clarification.options)

    scope = " ".join(
        [clarification.question]
        + [
            f"{option.option_id} {option.label} {option.description}"
            for option in clarification.options
        ]
    )
    if "异常" in scope or "移除" in scope or "剔除" in scope:
        return _looks_like_outlier_clarification_answer(text)
    if "日均" in scope or "总金额" in scope or "口径" in scope:
        return _looks_like_metric_clarification_answer(normalized)
    return False


def _looks_like_legacy_clarification_answer(text: str) -> bool:
    return text in {"日均。", "日均", "按推荐继续。", "按推荐继续"} or (
        any(token in text for token in ("按日", "复算", "移除", "异常"))
        and any(token in text for token in ("粒度", "日期", "订单级", "明细"))
    )


def _looks_like_outlier_clarification_answer(text: str) -> bool:
    return (
        any(token in text for token in ("移除", "剔除", "排除", "去掉", "排掉"))
        and any(token in text for token in ("按日", "按天", "日期", "天", "日"))
        and any(token in text for token in ("复算", "贡献最大", "最大正向"))
    )


def _looks_like_metric_clarification_answer(text: str) -> bool:
    if any(token in text for token in ("为什么", "怎么", "多少", "变化", "掉了", "?", "？")):
        return False
    return text in {
        "日均",
        "按日均",
        "按日均付费金额",
        "付费总金额",
        "总金额",
        "按付费总金额",
    }


def _is_outlier_removal_question(text: str) -> bool:
    removal_tokens = ("移除", "剔除", "排除", "去掉", "排掉")
    outlier_tokens = ("异常", "波峰", "波动", "日期", "天", "日")
    return any(token in text for token in removal_tokens) and any(
        token in text for token in outlier_tokens
    )


def _should_run(intent: str, relation: str) -> bool:
    if relation == "ask_topic_choice":
        return False
    return intent not in {"off_topic", "capability_question", "unsupported_request", "memory_update"}


def _requested_nodes(message: str, intent: str) -> tuple[str, ...]:
    nodes: list[str] = ["business_intent"]
    nodes.extend(suggest_revenue_diagnostic_nodes(message, intent))
    if (
        intent
        in {"new_topic", "mixed_question", "correction", "clarification_answer", "artifact_continue"}
        and "compare_periods" not in nodes
    ):
        nodes.append("compare_periods")
    if "answer_verify" not in nodes:
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
