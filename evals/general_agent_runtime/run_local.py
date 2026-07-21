from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlparse
from uuid import uuid4

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.durable_tool_bridge import PendingActionResolution
from bi_agent.runtime.general_agent_entry import (
    GeneralAgentTurnCommand,
    run_general_agent_turn,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = ROOT / "evals/general_agent_runtime/cases.jsonl"
_HAN_TEXT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict) or not value.get("caseId") or not value.get("turn"):
            raise ValueError(f"eval_case_invalid:{line_number}")
        cases.append(value)
    if not cases:
        raise ValueError("eval_cases_missing")
    return cases


def _create_thread(thread_id: str, actor_id: str) -> None:
    store = PostgresConversationStore.from_env()
    try:
        store.create_thread(thread_id, owner_id=actor_id)
    finally:
        store.connection.close()


def _inspect_operation(thread_id: str, operation_id: str) -> dict[str, Any]:
    store = PostgresConversationStore.from_env()
    try:
        ledger = store.thread_item_ledger
        selection_item = ledger.get_item_by_operation_key(
            thread_id,
            f"tool-selection:{operation_id}",
        )
        selection = (
            selection_item.payload.get("tool_selection", {})
            if selection_item is not None
            else {}
        )
        items = ledger.list_items(thread_id)
        tool_calls = [
            str(item.payload.get("sdk_item", {}).get("name") or "")
            for item in items
            if item.item_type == "tool_call"
            and str(item.operation_key or "").startswith(f"tool-call:{operation_id}:")
        ]
        task_rows = store.connection.execute(
            """
            SELECT run_id, status
            FROM waje_runtime.analysis_runs
            WHERE thread_id = %(thread_id)s
            ORDER BY created_at, run_id
            """,
            {"thread_id": thread_id},
        ).fetchall()
        return {
            "selection": dict(selection) if isinstance(selection, Mapping) else {},
            "toolCalls": [name for name in tool_calls if name],
            "tasks": [
                {
                    "taskRef": str(row.get("run_id") if isinstance(row, Mapping) else row[0]),
                    "status": str(row.get("status") if isinstance(row, Mapping) else row[1]),
                }
                for row in task_rows
            ],
        }
    finally:
        store.connection.close()


def _customer_text_is_zh(pending_action: Mapping[str, Any]) -> bool:
    values = [str(pending_action.get("prompt") or "")]
    for option in pending_action.get("options") or []:
        if isinstance(option, Mapping):
            values.extend((str(option.get("label") or ""), str(option.get("description") or "")))
    return bool(values) and all(_HAN_TEXT.search(value) for value in values)


def _evaluate(
    *,
    expected: Mapping[str, Any],
    result: Any,
    inspection: Mapping[str, Any],
    tasks_before: int,
) -> list[str]:
    failures: list[str] = []
    selection = inspection.get("selection") or {}
    tool_calls = list(inspection.get("toolCalls") or [])
    tasks = list(inspection.get("tasks") or [])
    initial_action = selection.get("initialAction")
    required_tool = selection.get("requiredToolName")
    if expected.get("materialDecisionTopics") is not None and (
        selection.get("materialDecisionTopics") != expected["materialDecisionTopics"]
    ):
        failures.append("material_decision_topics_mismatch")
    if expected.get("initialAction") and initial_action != expected["initialAction"]:
        failures.append("initial_action_mismatch")
    if expected.get("initialActionOneOf") and initial_action not in expected["initialActionOneOf"]:
        failures.append("initial_action_not_allowed")
    if expected.get("requiredTool"):
        wanted = expected["requiredTool"]
        if required_tool != wanted or not tool_calls or tool_calls[0] != wanted:
            failures.append("required_tool_mismatch")
    allowed_tools = expected.get("requiredToolOneOf") or []
    if initial_action == "call_tool" and allowed_tools and (
        required_tool not in allowed_tools or not tool_calls or tool_calls[0] not in allowed_tools
    ):
        failures.append("required_tool_not_allowed")
    if set(tool_calls) & set(expected.get("forbiddenTools") or []):
        failures.append("forbidden_tool_called")
    if expected.get("customerState") and result.status != expected["customerState"]:
        failures.append("customer_state_mismatch")
    if expected.get("customerStateOneOf") and result.status not in expected["customerStateOneOf"]:
        failures.append("customer_state_not_allowed")
    admission = result.terminal_admission
    completion_kind = admission.completion_kind if admission is not None else None
    if expected.get("completionKind") and completion_kind != expected["completionKind"]:
        failures.append("completion_kind_mismatch")
    if expected.get("completionKindOneOf") and completion_kind not in expected["completionKindOneOf"]:
        failures.append("completion_kind_not_allowed")
    if expected.get("authorityRequired") and (
        admission is None or not admission.authority_refs
    ):
        failures.append("authority_refs_missing")
    projection = result.customer_projection()
    pending = projection.get("pendingAction") or {}
    options = pending.get("options") or []
    option_count = expected.get("optionCount")
    if option_count and not (
        int(option_count["minimum"]) <= len(options) <= int(option_count["maximum"])
    ):
        failures.append("option_count_invalid")
    recommended = sum(
        option.get("recommended") is True for option in options if isinstance(option, Mapping)
    )
    if expected.get("recommendedOptionCount") is not None and recommended != expected["recommendedOptionCount"]:
        failures.append("recommended_option_count_invalid")
    if expected.get("customerLanguage") == "zh-Hans" and not _customer_text_is_zh(pending):
        failures.append("customer_language_mismatch")
    checkpoint = result.checkpoint_item.payload.get("checkpoint", {}) if result.checkpoint_item else {}
    if expected.get("checkpointKind") and checkpoint.get("checkpointKind") != expected["checkpointKind"]:
        failures.append("checkpoint_kind_mismatch")
    if expected.get("checkpointSchema") and checkpoint.get("schemaVersion") != expected["checkpointSchema"]:
        failures.append("checkpoint_schema_mismatch")
    if expected.get("checkpointSchema") and not checkpoint.get("actionBindingDigest"):
        failures.append("checkpoint_action_binding_missing")
    if set(expected.get("forbiddenTools") or []) & {"run_bi_analysis", "continue_bi_analysis"}:
        if len(tasks) != tasks_before:
            failures.append("forbidden_task_created")
    return failures


async def _run_case(
    case: Mapping[str, Any],
    *,
    actor_id: str,
    completed_thread_id: str | None,
    run_ref: str,
) -> dict[str, Any]:
    case_id = str(case["caseId"])
    if case.get("threadMode") == "completed_analysis":
        if not completed_thread_id:
            raise ValueError("completed_analysis_thread_required")
        thread_id = completed_thread_id
    else:
        thread_id = f"thread-eval-{case_id}-{run_ref}"
        _create_thread(thread_id, actor_id)
    operation_id = f"eval-{case_id}-{run_ref}"
    before = _inspect_operation(thread_id, operation_id)
    result = await run_general_agent_turn(
        GeneralAgentTurnCommand(
            threadId=thread_id,
            actorId=actor_id,
            operationId=operation_id,
            message=str(case["turn"]),
        )
    )
    inspection = _inspect_operation(thread_id, operation_id)
    failures = _evaluate(
        expected=case["expected"],
        result=result,
        inspection=inspection,
        tasks_before=len(before["tasks"]),
    )
    follow_up_report: dict[str, Any] | None = None
    follow_up = case.get("followUp")
    if isinstance(follow_up, Mapping):
        pending = result.customer_projection().get("pendingAction") or {}
        recommended = next(
            (option for option in pending.get("options") or [] if option.get("recommended") is True),
            None,
        )
        if not isinstance(recommended, Mapping):
            failures.append("recommended_follow_up_missing")
        else:
            answer = f"采用推荐项：{recommended['label']}"
            follow_operation = operation_id + "-follow-up"
            follow_result = await run_general_agent_turn(
                GeneralAgentTurnCommand(
                    threadId=thread_id,
                    actorId=actor_id,
                    operationId=follow_operation,
                    message=answer,
                    pendingActionResolution=PendingActionResolution(
                        actionRef=str(pending["actionRef"]),
                        decision="answered",
                        selectedOptionId=str(recommended["optionId"]),
                        answerText=answer,
                    ),
                )
            )
            follow_inspection = _inspect_operation(thread_id, follow_operation)
            follow_failures = _evaluate(
                expected=follow_up["expected"],
                result=follow_result,
                inspection=follow_inspection,
                tasks_before=len(inspection["tasks"]),
            )
            failures.extend(f"follow_up:{failure}" for failure in follow_failures)
            follow_up_report = {
                "status": follow_result.status,
                "selection": follow_inspection["selection"],
                "toolCalls": follow_inspection["toolCalls"],
                "failures": follow_failures,
            }
    return {
        "caseId": case_id,
        "threadId": thread_id,
        "status": "passed" if not failures else "failed",
        "runtimeStatus": result.status,
        "selection": inspection["selection"],
        "toolCalls": inspection["toolCalls"],
        "taskCount": len(inspection["tasks"]),
        "failures": failures,
        "followUp": follow_up_report,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    os.environ["WAJE_RUNTIME_DATABASE_URL"] = args.database_url
    os.environ.pop("OPENAI_API_KEY", None)
    origin = urlparse(os.environ.get("WAJE_LLM_BASE_URL", ""))
    if origin.hostname in {None, "api.openai.com"}:
        raise ValueError("eval_provider_outbound_origin_invalid")
    run_ref = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + uuid4().hex[:6]
    cases = _load_cases(args.cases)
    selected = set(args.case_id or [])
    if selected:
        cases = [case for case in cases if case["caseId"] in selected]
    results = []
    for case in cases:
        try:
            results.append(
                await _run_case(
                    case,
                    actor_id=args.actor_id,
                    completed_thread_id=args.completed_thread_id,
                    run_ref=run_ref,
                )
            )
        except Exception as exc:
            results.append(
                {
                    "caseId": case["caseId"],
                    "status": "failed",
                    "failures": [str(getattr(exc, "code", "") or type(exc).__name__)],
                }
            )
    passed = sum(result["status"] == "passed" for result in results)
    return {
        "schemaVersion": "general-agent-runtime-live-eval.v2",
        "status": "passed" if passed == len(results) else "failed",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "provider": os.environ.get("WAJE_LLM_PROVIDER", ""),
        "model": os.environ.get("WAJE_LLM_MODEL", ""),
        "outboundOrigin": f"{origin.scheme}://{origin.netloc}",
        "openAiApiKeyPresent": False,
        "openAiHostedRequestCount": 0,
        "caseCount": len(results),
        "passedCaseCount": passed,
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--completed-thread-id")
    parser.add_argument("--actor-id", default="local-user")
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(_run(args))
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
