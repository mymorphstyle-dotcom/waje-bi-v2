from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Optional
from uuid import uuid4

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.conversation.runtime import ConversationRuntime
from bi_agent.conversation.store import InMemoryConversationStore
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
    ) -> dict[str, Any]:
        role = str((permission_context or {}).get("role") or role or "analyst")
        run_id = run_id or f"run-{uuid4().hex[:12]}"
        self.store.upsert_run(run_id, thread_id=thread_id, status="running")
        turn = ConversationRuntime(
            self.store,
            llm_client=self.conversation_llm_client,
        ).handle_message(thread_id, user_message, role=role)
        context_manifest = turn.context_manifest.to_dict()
        self.store.record_context_manifest(context_manifest)

        if not turn.run_request:
            if turn.needs_clarification:
                request = {
                    "reason": "needs_clarification",
                    "intent": turn.turn_intent.intent,
                    "clarification": turn.clarification.to_dict() if turn.clarification else None,
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
        request.update(
            {
                "run_id": run_id,
                "question": user_message,
                "role": role,
                "user_id": user_id,
                "permission_context": permission_context or {},
                "artifact_root": artifact_root,
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
                "failure_reason": result.failure_reason,
            }

        package = dict(result.answer_package)
        package["run_id"] = run_id
        package["artifact_path"] = result.artifact_path
        accepted_graph = (
            package.get("accepted_graph")
            or package.get("admin_audit", {}).get("accepted_graph")
            or request.get("requested_nodes", [])
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
            "quality_review": package.get("admin_audit"),
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
    answer_text = _dry_run_answer_text(question)
    return WorkflowRunResult(
        status="draft",
        run_id=run_id,
        answer_package={
            "run_id": run_id,
            "status": "draft",
            "snapshot_id": "dry-run",
            "permission_scope": str(request.get("role") or "analyst"),
            "follow_up_context": "dry-run harness artifact",
            "sections": [
                {
                    "id": "summary",
                    "visibility": "business_summary",
                    "payload": {"answer_text": answer_text},
                }
            ],
            "accepted_graph": requested_nodes,
            "llm_calls": [],
            "admin_audit": {"verifier": {"status": "skipped_dry_run"}, "accepted_graph": requested_nodes},
        },
        artifact_path=f"{request.get('artifact_root', 'artifacts/phase-7')}/{run_id}.json",
        checkpoint_events=({"node": "dry_run_workflow", "status": "completed"},),
    )


def _dry_run_answer_text(question: str) -> str:
    if "WajeSpecial" in question:
        return "演练回答：WajeSpecial 只能作为候选解释，还不能直接说成主要原因。"
    if "移除" in question or "异常" in question or "复算" in question:
        return "演练回答：已按聚合口径移除异常影响后复算，用来判断方向是否仍成立。"
    return f"演练回答：{question}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--role", default="analyst")
    parser.add_argument("--artifact-root", default="artifacts/phase-7")
    args = parser.parse_args(argv)

    store = PostgresConversationStore.from_env()
    core = ConversationAgentCore(store, conversation_llm_client=_conversation_llm_from_env())
    result = core.run_message(
        thread_id=args.thread_id,
        run_id=args.run_id,
        user_message=args.message,
        role=args.role,
        artifact_root=args.artifact_root,
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
