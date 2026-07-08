from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Optional
from uuid import uuid4

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.conversation.runtime import ConversationRuntime
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.compiler import compile_graph
from bi_agent.runtime.langgraph_workflow import WorkflowRunResult, run_pattern_workflow


WorkflowRunner = Callable[[dict[str, Any]], Any]


class ConversationAgentCore:
    def __init__(
        self,
        store: Any,
        *,
        workflow_runner: Optional[WorkflowRunner] = None,
        conversation_llm_client: Any = None,
    ) -> None:
        self.store = store
        self.workflow_runner = workflow_runner or run_pattern_workflow
        self.conversation_llm_client = conversation_llm_client

    def run_message(
        self,
        *,
        thread_id: str,
        run_id: str | None = None,
        user_message: str,
        user_id: str | None = None,
        permission_context: dict | None = None,
        role: str = "analyst",
        artifact_root: str = "artifacts/phase-7",
        clarification: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        role = str((permission_context or {}).get("role") or role or "analyst")
        run_id = run_id or f"run-{uuid4().hex[:12]}"
        self.store.upsert_run(run_id, thread_id=thread_id, status="running")
        if clarification:
            self.store.add_audit_event(
                "clarification_answer_submitted",
                thread_id=thread_id,
                run_id=run_id,
                ref=run_id,
                payload=clarification,
            )
        turn = ConversationRuntime(
            self.store,
            llm_client=self.conversation_llm_client,
        ).handle_message(thread_id, user_message, role=role, run_id=run_id)
        context_manifest = turn.context_manifest.to_dict()
        if turn.run_request and self.workflow_runner is _dry_run_workflow:
            context_manifest = _manifest_with_dry_run_source(context_manifest, run_id, role)
        self.store.record_context_manifest(context_manifest)

        if not turn.run_request:
            if turn.needs_clarification:
                request = {
                    "reason": "needs_clarification",
                    "intent": turn.turn_intent.intent,
                    "clarification": turn.clarification.to_dict() if turn.clarification else None,
                    "clarification_answer": clarification,
                    "user_id": user_id,
                    "permission_context": permission_context or {},
                }
                self.store.upsert_run(
                    run_id,
                    thread_id=thread_id,
                    turn_id=turn.turn_id,
                    topic_id=turn.topic_id or "",
                    status="waiting_for_clarification",
                    request=request,
                )
                self.store.add_audit_event(
                    "clarification_requested",
                    thread_id=thread_id,
                    topic_id=turn.topic_id or "",
                    run_id=run_id,
                    ref=run_id,
                    payload=request["clarification"] or {},
                )
                return {
                    "status": "waiting_for_clarification",
                    "run_id": run_id,
                    "turn_id": turn.turn_id,
                    "topic_id": turn.topic_id,
                    "intent": turn.turn_intent.intent,
                    "topic_relation": turn.topic_relation,
                    "clarification": request["clarification"],
                    "context_manifest": context_manifest,
                }
            self.store.upsert_run(
                run_id,
                thread_id=thread_id,
                turn_id=turn.turn_id,
                topic_id=turn.topic_id or "",
                status="completed_without_workflow",
                request={"reason": turn.turn_intent.intent},
            )
            return {
                "status": "completed_without_workflow",
                "run_id": run_id,
                "turn_id": turn.turn_id,
                "topic_id": turn.topic_id,
                "intent": turn.turn_intent.intent,
                "topic_relation": turn.topic_relation,
                "context_manifest": context_manifest,
            }

        request = turn.run_request.to_dict()
        request["context_manifest"] = context_manifest
        request["reuse_decisions"] = [decision.to_dict() for decision in turn.reuse_decisions]
        request.update(
            {
                "run_id": run_id,
                "question": user_message,
                "role": role,
                "user_id": user_id,
                "permission_context": permission_context or {},
                "artifact_root": artifact_root,
                "clarification_answer": clarification,
            }
        )
        self.store.upsert_run(
            run_id,
            thread_id=thread_id,
            turn_id=turn.turn_id,
            topic_id=turn.topic_id or "",
            status="running_workflow",
            request=request,
        )
        result = self.workflow_runner(request)
        if result.status != "draft" or not result.answer_package:
            self.store.upsert_run(
                run_id,
                thread_id=thread_id,
                turn_id=turn.turn_id,
                topic_id=turn.topic_id or "",
                status="failed",
                request={**request, "failure_reason": result.failure_reason},
            )
            self.store.add_audit_event(
                "workflow_failed",
                thread_id=thread_id,
                topic_id=turn.topic_id or "",
                run_id=run_id,
                payload={"failure_reason": result.failure_reason},
            )
            return {
                "status": "failed",
                "run_id": run_id,
                "turn_id": turn.turn_id,
                "topic_id": turn.topic_id,
                "intent": turn.turn_intent.intent,
                "topic_relation": turn.topic_relation,
                "context_manifest": context_manifest,
                "failure_reason": result.failure_reason,
            }

        package = dict(result.answer_package)
        package["run_id"] = run_id
        package["artifact_path"] = result.artifact_path
        accepted_graph = (
            package.get("accepted_graph")
            or package.get("admin_audit", {}).get("accepted_graph")
            or []
        )
        self.store.record_run_nodes(run_id, tuple(result.checkpoint_events))
        self.store.record_answer_package(run_id, package)
        self.store.upsert_run(
            run_id,
            thread_id=thread_id,
            turn_id=turn.turn_id,
            topic_id=turn.topic_id or "",
            status="completed",
            request=request,
        )
        return {
            "status": "completed",
            "run_id": run_id,
            "turn_id": turn.turn_id,
            "topic_id": turn.topic_id,
            "intent": turn.turn_intent.intent,
            "topic_relation": turn.topic_relation,
            "artifact_path": result.artifact_path,
            "answer_package": package,
            "context_manifest": context_manifest,
            "accepted_graph": accepted_graph,
            "llm_calls": package.get("llm_calls", []),
            "quality_review": package.get("quality_gate") or package.get("admin_audit"),
        }

    @classmethod
    def from_environment(
        cls,
        *,
        real_llm: bool = False,
        real_clickhouse: bool = False,
    ) -> "ConversationAgentCore":
        if real_llm or real_clickhouse:
            return cls(
                PostgresConversationStore.from_env(),
                conversation_llm_client=_conversation_llm_from_env() if real_llm else None,
            )
        return cls(InMemoryConversationStore(), workflow_runner=_dry_run_workflow)


def _conversation_llm_from_env() -> Any:
    try:
        from bi_agent.runtime.llm_client import OpenAICompatibleLLMClient

        return OpenAICompatibleLLMClient.from_env()
    except Exception:
        return None


def _dry_run_workflow(request: dict[str, Any]) -> WorkflowRunResult:
    run_id = str(request.get("run_id") or f"run-{uuid4().hex[:12]}")
    question = str(request.get("question") or request.get("user_message") or "")
    requested_nodes = list(request.get("requested_nodes") or [])
    compiled = _compile_dry_run_graph(requested_nodes)
    accepted_graph = list(compiled.mutations.accepted_graph)
    answer_text = _dry_run_answer_text(question)
    final_answer = _dry_run_final_answer(answer_text, accepted_graph)
    quality_gate = _dry_run_quality_gate(answer_text, final_answer)
    evidence_ref = _dry_run_claim_source_ref(request)
    return WorkflowRunResult(
        status="draft",
        run_id=run_id,
        answer_package={
            "run_id": run_id,
            "status": "draft",
            "snapshot_id": "dry-run",
            "permission_scope": str(request.get("role") or "analyst"),
            "follow_up_context": "dry-run harness artifact",
            "final_answer": final_answer,
            "follow_up_questions": _dry_run_follow_up_questions(accepted_graph),
            "quality_gate": quality_gate,
            "sections": [
                {
                    "id": "summary",
                    "visibility": "business_summary",
                    "payload": {
                        "answer_text": answer_text,
                        "claims": [
                            {
                                "text": answer_text,
                                "claim_strength": "dry_run_context",
                                "evidence_refs": [evidence_ref],
                            }
                        ],
                    },
                },
                {
                    "id": "evidence",
                    "visibility": "aggregate_evidence",
                    "payload": {
                        "evidence": [
                            {
                                "evidence_ref": evidence_ref,
                                "evidence_type": "artifact",
                                "strength": "dry_run",
                                "artifact_ref": f"artifacts/phase-7/{run_id}.json",
                            }
                        ]
                    },
                }
            ],
            "accepted_graph": accepted_graph,
            "llm_calls": [],
            "admin_audit": {
                "verifier": {"status": "skipped_dry_run"},
                "accepted_graph": accepted_graph,
                "compiler_status": compiled.status,
                "compiler_mutations": {
                    "proposed_graph": list(compiled.mutations.proposed_graph),
                    "accepted_graph": accepted_graph,
                    "rejected_or_degraded": list(compiled.mutations.rejected_or_degraded),
                },
            },
        },
        artifact_path=f"{request.get('artifact_root', 'artifacts/phase-7')}/{run_id}.json",
        checkpoint_events=({"node": "dry_run_workflow", "status": "completed"},),
    )


def _compile_dry_run_graph(requested_nodes: list[str]):
    families = ["paid_amount_change_explanation"]
    if any(node in requested_nodes for node in ("segment_contribution", "joint_attribution")):
        families.append("segment_or_factor_attribution")
    if any(node in requested_nodes for node in ("outlier_scan", "outlier_contribution")):
        families.append("anomaly_or_black_swan_review")
    return compile_graph(
        question_family=families[0],
        target_metric="payment_amount",
        requested_nodes=requested_nodes,
        question_families=families,
    )


def _dry_run_answer_text(question: str) -> str:
    if "WajeSpecial" in question:
        return "演练回答：WajeSpecial 只能作为候选解释，还不能直接说成主要原因。"
    if "移除" in question or "异常" in question or "复算" in question:
        return "演练回答：已按聚合口径移除异常影响后复算，用来判断方向是否仍成立。"
    return f"演练回答：{question}"


def _dry_run_final_answer(answer_text: str, accepted_graph: list[str]) -> str:
    focus = _dry_run_focus(accepted_graph)
    return (
        f"最终结论：{answer_text} 当前证据能把排查方向收敛到{focus}；"
        "还不能直接说这是唯一原因或已被因果证明。"
    )


def _dry_run_focus(accepted_graph: list[str]) -> str:
    labels = {
        "driver_decomposition": "用户数和人均付费拆解",
        "segment_contribution": "渠道贡献",
        "joint_attribution": "渠道和阶段组合贡献",
        "outlier_scan": "异常日期识别",
        "outlier_contribution": "异常日期移除复核",
        "event_evidence": "活动事件证据",
        "compare_periods": "目标窗口和基线窗口对比",
        "answer_verify": "答案边界校验",
    }
    selected = [labels[node] for node in accepted_graph if node in labels]
    return "、".join(selected) if selected else "当前聚合证据"


def _dry_run_quality_gate(answer_text: str, final_answer: str) -> dict[str, Any]:
    verified_claim_preserved = answer_text in final_answer
    business_insight_present = "当前证据能把排查方向收敛到" in final_answer
    direct_answer = "最终结论" in final_answer
    issues: list[str] = []
    if not direct_answer:
        issues.append("missing_direct_answer")
    if not verified_claim_preserved:
        issues.append("missing_verified_claim")
    if not business_insight_present:
        issues.append("missing_business_insight")
    return {
        "direct_answer": direct_answer,
        "has_verified_claims": True,
        "verified_claim_preserved": verified_claim_preserved,
        "business_insight_present": business_insight_present,
        "followups_one_intent": True,
        "issues": issues,
    }


def _dry_run_follow_up_questions(accepted_graph: list[str]) -> list[str]:
    if "outlier_contribution" in accepted_graph:
        return [
            "要复核移除异常日期后的贡献变化吗？",
            "要看异常日期集中在哪些业务窗口吗？",
            "要继续检查渠道贡献是否稳定吗？",
        ]
    if "joint_attribution" in accepted_graph or "segment_contribution" in accepted_graph:
        return [
            "要先看哪个渠道的贡献最稳定吗？",
            "要复核异常日期剔除后的方向吗？",
            "要把新老用户贡献单独拆开看吗？",
        ]
    return [
        "要继续看贡献最大的业务因素吗？",
        "要复核异常日期对结果的影响吗？",
        "要换成日均口径再算一次吗？",
    ]


def _manifest_with_dry_run_source(manifest: dict[str, Any], run_id: str, role: str) -> dict[str, Any]:
    ref = f"artifact:context:{manifest.get('manifest_id', run_id)}"
    updated = dict(manifest)
    items = list(updated.get("items") or [])
    existing = {str(item.get("source_ref")) for item in items if isinstance(item, dict)}
    if ref not in existing:
        items.append(
            {
                "source_type": "artifact",
                "source_ref": ref,
                "summary": "dry-run harness 预置的本轮上下文 artifact 引用。",
                "can_support_claims": True,
                "visibility": "analyst",
                "reason": "dry_run_claim_source",
                "permission_scope": role,
                "source_version": "dry-run",
                "expired": False,
                "claim_use": "evidence",
            }
        )
    updated["items"] = items
    updated["can_support_claims"] = True
    return updated


def _dry_run_claim_source_ref(request: dict[str, Any]) -> str:
    manifest = request.get("context_manifest") or {}
    for item in manifest.get("items", []):
        if (
            isinstance(item, dict)
            and item.get("can_support_claims")
            and item.get("claim_use") == "evidence"
            and str(item.get("source_ref", "")).startswith("artifact:")
        ):
            return str(item["source_ref"])
    return f"artifact:context:{request.get('run_id', 'dry_run')}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--role", default="analyst")
    parser.add_argument("--artifact-root", default="artifacts/phase-7")
    parser.add_argument("--clarification")
    args = parser.parse_args(argv)
    clarification = json.loads(args.clarification) if args.clarification else None

    store = PostgresConversationStore.from_env()
    core = ConversationAgentCore(store, conversation_llm_client=_conversation_llm_from_env())
    result = core.run_message(
        thread_id=args.thread_id,
        run_id=args.run_id,
        user_message=args.message,
        role=args.role,
        artifact_root=args.artifact_root,
        clarification=clarification,
    )
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result["status"] in {
        "completed",
        "completed_without_workflow",
        "waiting_for_clarification",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
