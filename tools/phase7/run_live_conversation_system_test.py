from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bi_agent.conversation.agent_core import ConversationAgentCore


def load_cases(path: str) -> list[dict[str, Any]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return []
    cases = raw.get("conversation_cases", [])
    return [case for case in cases if isinstance(case, dict) and case.get("id")]


def select_cases(cases: list[dict[str, Any]], case_id: str | None) -> list[dict[str, Any]]:
    if not case_id:
        return cases
    return [case for case in cases if case["id"] == case_id]


def _effective_result(turn_record: dict[str, Any]) -> dict[str, Any]:
    if turn_record.get("resumed_status"):
        return {
            "status": turn_record.get("resumed_status"),
            "run_id": turn_record.get("resumed_run_id"),
            "topic_id": turn_record.get("resumed_topic_id"),
            "answer_package": turn_record.get("resumed_answer_package"),
            "context_manifest": turn_record.get("resumed_context_manifest"),
            "accepted_graph": turn_record.get("resumed_accepted_graph") or [],
            "llm_calls": turn_record.get("resumed_llm_calls", []),
            "quality_review": turn_record.get("resumed_quality_review"),
        }
    return {
        "status": turn_record.get("status"),
        "run_id": turn_record.get("run_id"),
        "topic_id": turn_record.get("topic_id"),
        "answer_package": turn_record.get("answer_package"),
        "context_manifest": turn_record.get("context_manifest"),
        "accepted_graph": turn_record.get("accepted_graph") or [],
        "llm_calls": turn_record.get("llm_calls", []),
        "quality_review": turn_record.get("quality_review"),
    }


def _review_expectations(turn: dict[str, Any], effective_graph: list[str] | tuple[str, ...]) -> dict[str, Any]:
    required = list((turn.get("expect") or {}).get("required_capabilities", []))
    actual = list(effective_graph or [])
    missing = [capability for capability in required if capability not in actual]
    return {
        "required_capabilities": required,
        "missing_required_capabilities": missing,
        "passed": not missing,
    }


def _missing_inputs_from_error(exc: Exception) -> list[str]:
    text = str(exc)
    if "WAJE_RUNTIME_DATABASE_URL or DATABASE_URL" in text:
        return ["WAJE_RUNTIME_DATABASE_URL", "DATABASE_URL"]
    return []


def run_case(core: ConversationAgentCore, case: dict[str, Any], artifact_dir: Path) -> dict[str, Any]:
    thread_id = f"live-{case['id']}"
    turns: list[dict[str, Any]] = []
    for index, turn in enumerate(case["turns"], start=1):
        result = core.run_message(thread_id=thread_id, user_message=turn["user"])
        turn_record = {
            "index": index,
            "user": turn["user"],
            "status": result["status"],
            "run_id": result["run_id"],
            "topic_id": result.get("topic_id"),
            "answer_package": result.get("answer_package"),
            "context_manifest": result.get("context_manifest"),
            "accepted_graph": result.get("accepted_graph"),
            "llm_calls": result.get("llm_calls", []),
            "quality_review": result.get("quality_review"),
        }
        if result["status"] == "waiting_for_clarification" and turn.get("clarification_response"):
            resumed = core.run_message(
                thread_id=thread_id,
                user_message=turn["clarification_response"],
            )
            turn_record["clarification_response"] = turn["clarification_response"]
            turn_record["resumed_status"] = resumed["status"]
            turn_record["resumed_run_id"] = resumed["run_id"]
            turn_record["resumed_topic_id"] = resumed.get("topic_id")
            turn_record["resumed_answer_package"] = resumed.get("answer_package")
            turn_record["resumed_context_manifest"] = resumed.get("context_manifest")
            turn_record["resumed_accepted_graph"] = resumed.get("accepted_graph")
            turn_record["resumed_llm_calls"] = resumed.get("llm_calls", [])
            turn_record["resumed_quality_review"] = resumed.get("quality_review")
        effective = _effective_result(turn_record)
        turn_record["expectation_review"] = _review_expectations(
            turn,
            effective.get("accepted_graph") or [],
        )
        turns.append(turn_record)
    final_result = _effective_result(turns[-1]) if turns else {}
    output = {
        "case_id": case["id"],
        "status": "failed" if any(not turn["expectation_review"]["passed"] for turn in turns) else "passed",
        "final_turn_status": final_result.get("status"),
        "run_id": final_result.get("run_id"),
        "topic_id": final_result.get("topic_id"),
        "answer_package": final_result.get("answer_package"),
        "context_manifest": final_result.get("context_manifest"),
        "accepted_graph": final_result.get("accepted_graph") or [],
        "llm_calls": final_result.get("llm_calls", []),
        "quality_review": final_result.get("quality_review"),
        "turns": turns,
    }
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / f"{case['id']}.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="evals/phase7/conversation_scenarios.yaml")
    parser.add_argument("--case")
    parser.add_argument("--artifact-dir", default="artifacts/phase7/live-conversation")
    parser.add_argument("--real-llm", action="store_true")
    parser.add_argument("--real-clickhouse", action="store_true")
    args = parser.parse_args()

    selected = select_cases(load_cases(args.cases), args.case)
    try:
        core = ConversationAgentCore.from_environment(
            real_llm=args.real_llm,
            real_clickhouse=args.real_clickhouse,
        )
        results = [run_case(core, case, Path(args.artifact_dir)) for case in selected]
    except RuntimeError as exc:
        artifact_dir = Path(args.artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_name = f"{args.case}.json" if args.case else "environment_blocked.json"
        blocked = {
            "status": "blocked",
            "missing_inputs": _missing_inputs_from_error(exc),
            "owner": "local runtime/deployment owner",
            "error": str(exc),
        }
        (artifact_dir / artifact_name).write_text(
            json.dumps(blocked, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise
    print(
        json.dumps(
            {"case_count": len(results), "case_ids": [case["case_id"] for case in results]},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
