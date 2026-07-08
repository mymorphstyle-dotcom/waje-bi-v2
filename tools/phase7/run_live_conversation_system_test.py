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
    if isinstance(raw, dict):
        cases = raw.get("cases", [])
    else:
        cases = raw or []
    return [case for case in cases if isinstance(case, dict) and case.get("id")]


def select_cases(cases: list[dict[str, Any]], case_id: str | None) -> list[dict[str, Any]]:
    if not case_id:
        return cases
    return [case for case in cases if case["id"] == case_id]


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
        turns.append(turn_record)
    output = {"case_id": case["id"], "turns": turns}
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

    core = ConversationAgentCore.from_environment(
        real_llm=args.real_llm,
        real_clickhouse=args.real_clickhouse,
    )
    selected = select_cases(load_cases(args.cases), args.case)
    results = [run_case(core, case, Path(args.artifact_dir)) for case in selected]
    print(
        json.dumps(
            {"case_count": len(results), "case_ids": [case["case_id"] for case in results]},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
