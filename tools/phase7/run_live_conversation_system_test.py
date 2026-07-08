from __future__ import annotations

import argparse
import json
import os
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


def load_env_file(path: str = ".env") -> list[str]:
    env_path = Path(path)
    if not env_path.exists():
        return []
    loaded: list[str] = []
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _strip_env_value(value.strip())
        loaded.append(key)
    return loaded


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    if " #" in value:
        return value.split(" #", 1)[0].strip()
    return value


def _effective_result(turn_record: dict[str, Any]) -> dict[str, Any]:
    if turn_record.get("resumed_status"):
        return {
            "status": turn_record.get("resumed_status"),
            "run_id": turn_record.get("resumed_run_id"),
            "topic_id": turn_record.get("resumed_topic_id"),
            "intent": turn_record.get("resumed_intent"),
            "topic_relation": turn_record.get("resumed_topic_relation"),
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
        "intent": turn_record.get("intent"),
        "topic_relation": turn_record.get("topic_relation"),
        "answer_package": turn_record.get("answer_package"),
        "context_manifest": turn_record.get("context_manifest"),
        "accepted_graph": turn_record.get("accepted_graph") or [],
        "llm_calls": turn_record.get("llm_calls", []),
        "quality_review": turn_record.get("quality_review"),
    }


def _review_expectations(turn: dict[str, Any], turn_record: dict[str, Any]) -> dict[str, Any]:
    effective = _effective_result(turn_record)
    return _expectation_review(turn, turn_record, effective, effective.get("accepted_graph") or [])


def _expectation_review(
    turn: dict[str, Any],
    turn_record: dict[str, Any],
    effective_result: dict[str, Any],
    effective_graph: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    expect = turn.get("expect") or {}
    required = list(expect.get("required_capabilities", []))
    actual = list(effective_graph or [])
    missing = [capability for capability in required if capability not in actual]
    actual_intent = str(turn_record.get("intent") or effective_result.get("intent") or "")
    actual_relation = str(
        turn_record.get("topic_relation") or effective_result.get("topic_relation") or ""
    )
    missing_answer_text = [
        text
        for text in expect.get("final_answer_contains", [])
        if text not in _answer_text(effective_result.get("answer_package") or {})
    ]
    manifest = effective_result.get("context_manifest")
    manifest_present = isinstance(manifest, dict) and bool(manifest)
    claim_review = _claim_evidence_review(
        effective_result.get("answer_package") or {},
        manifest if isinstance(manifest, dict) else {},
        requires_claims=bool(expect.get("final_answer_contains")),
    )
    manifest_can_support_claims = bool(manifest.get("can_support_claims")) if isinstance(manifest, dict) else False
    claim_support_ok = manifest_present and manifest_can_support_claims and claim_review["passed"]
    clarification_ok = True
    if expect.get("allow_clarification"):
        clarification_ok = (
            turn_record.get("status") == "waiting_for_clarification"
            and bool(turn_record.get("clarification_response"))
            and bool(turn_record.get("resumed_status"))
        )
    intent_ok = not expect.get("intent") or actual_intent == expect.get("intent")
    relation_ok = _topic_relation_matches(expect.get("topic_relation"), actual_relation)
    return {
        "expected_intent": expect.get("intent"),
        "actual_intent": actual_intent,
        "intent_passed": intent_ok,
        "expected_topic_relation": expect.get("topic_relation"),
        "actual_topic_relation": actual_relation,
        "topic_relation_passed": relation_ok,
        "allow_clarification": bool(expect.get("allow_clarification")),
        "clarification_passed": clarification_ok,
        "final_answer_contains": list(expect.get("final_answer_contains", [])),
        "missing_final_answer_text": missing_answer_text,
        "context_manifest_present": manifest_present,
        "context_manifest_can_support_claims": manifest_can_support_claims,
        "claim_support_policy_passed": claim_support_ok,
        "claim_evidence_review": claim_review,
        "required_capabilities": required,
        "missing_required_capabilities": missing,
        "passed": (
            intent_ok
            and relation_ok
            and clarification_ok
            and manifest_present
            and claim_support_ok
            and not missing
            and not missing_answer_text
        ),
    }


def _topic_relation_matches(expected: str | None, actual: str) -> bool:
    if not expected:
        return True
    aliases = {
        "create": {"new_topic"},
        "inherit": {"inherit_current"},
    }
    return actual == expected or actual in aliases.get(expected, set())


def _answer_text(answer_package: dict[str, Any]) -> str:
    parts: list[str] = []
    for section in answer_package.get("sections", []):
        payload = section.get("payload", {}) if isinstance(section, dict) else {}
        for key in ("answer_text", "final_business_summary"):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def _claim_evidence_review(
    answer_package: dict[str, Any],
    context_manifest: dict[str, Any],
    *,
    requires_claims: bool,
) -> dict[str, Any]:
    claims = _claims(answer_package)
    traceable_refs = _traceable_refs(answer_package, context_manifest)
    missing_claim_refs: list[int] = []
    unsupported_refs: list[str] = []
    for index, claim in enumerate(claims):
        refs = [str(ref) for ref in claim.get("evidence_refs", []) if ref]
        if not refs:
            missing_claim_refs.append(index)
        for ref in refs:
            if ref not in traceable_refs:
                unsupported_refs.append(ref)
    return {
        "claim_count": len(claims),
        "traceable_refs": sorted(traceable_refs),
        "missing_claim_ref_indexes": missing_claim_refs,
        "unsupported_evidence_refs": sorted(set(unsupported_refs)),
        "passed": (
            (not requires_claims or bool(claims))
            and not missing_claim_refs
            and not unsupported_refs
        ),
    }


def _claims(answer_package: dict[str, Any]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for section in answer_package.get("sections", []):
        payload = section.get("payload", {}) if isinstance(section, dict) else {}
        section_claims = payload.get("claims")
        if isinstance(section_claims, list):
            claims.extend(claim for claim in section_claims if isinstance(claim, dict))
    return claims


def _traceable_refs(answer_package: dict[str, Any], context_manifest: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for item in context_manifest.get("items", []):
        if isinstance(item, dict) and item.get("source_ref"):
            refs.add(str(item["source_ref"]))
    return refs


def _missing_inputs_from_error(exc: Exception, *, real_llm: bool = False, real_clickhouse: bool = False) -> list[str]:
    text = str(exc)
    missing: list[str] = []
    if "WAJE_RUNTIME_DATABASE_URL or DATABASE_URL" in text:
        missing.extend(["WAJE_RUNTIME_DATABASE_URL", "DATABASE_URL"])
    if real_llm:
        if not os.environ.get("WAJE_LLM_MODEL"):
            missing.append("WAJE_LLM_MODEL")
        if not (
            os.environ.get("WAJE_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
        ):
            missing.extend(["WAJE_LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"])
    if real_clickhouse:
        for key in (
            "WAJE_CLICKHOUSE_HOST",
            "WAJE_CLICKHOUSE_PORT",
            "WAJE_CLICKHOUSE_USER",
            "WAJE_CLICKHOUSE_PASSWORD",
            "WAJE_CLICKHOUSE_DATABASE",
            "WAJE_CLICKHOUSE_SECURE",
        ):
            if not os.environ.get(key):
                missing.append(key)
    return list(dict.fromkeys(missing))


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
            "intent": result.get("intent"),
            "topic_relation": result.get("topic_relation"),
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
            turn_record["resumed_intent"] = resumed.get("intent")
            turn_record["resumed_topic_relation"] = resumed.get("topic_relation")
            turn_record["resumed_answer_package"] = resumed.get("answer_package")
            turn_record["resumed_context_manifest"] = resumed.get("context_manifest")
            turn_record["resumed_accepted_graph"] = resumed.get("accepted_graph")
            turn_record["resumed_llm_calls"] = resumed.get("llm_calls", [])
            turn_record["resumed_quality_review"] = resumed.get("quality_review")
        turn_record["expectation_review"] = _review_expectations(turn, turn_record)
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

    load_env_file()
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
        case_id = args.case or "environment_blocked"
        blocked = {
            "case_id": case_id,
            "status": "blocked",
            "final_turn_status": "blocked",
            "run_id": None,
            "topic_id": None,
            "answer_package": None,
            "context_manifest": None,
            "accepted_graph": [],
            "llm_calls": [],
            "quality_review": None,
            "turns": [],
            "missing_inputs": _missing_inputs_from_error(
                exc,
                real_llm=args.real_llm,
                real_clickhouse=args.real_clickhouse,
            ),
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
