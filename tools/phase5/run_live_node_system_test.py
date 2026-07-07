#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Optional, Sequence

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bi_agent.runtime import langgraph_workflow as workflow
from bi_agent.runtime.artifacts import to_jsonable
from tools.phase5 import debug_node_runner as node_runner


DEFAULT_CASE_FILE = ROOT / "evals" / "phase5" / "live_node_system_cases.yaml"
DEFAULT_ARTIFACT_ROOT = ROOT / "artifacts" / "phase-5" / "live-node-system"

FIXED_NEXT = {
    "understand_business_intent": "decide_question_boundary",
    "decide_question_boundary": "clarification_policy_gate",
    "rebind_after_clarification": "decide_question_boundary",
    "confirm_business_understanding": "design_analysis_route",
    "design_analysis_route": "accept_analysis_route",
    "repair_analysis_route": "accept_analysis_route",
    "inspect_schema": "validate_runtime_binding",
    "validate_runtime_binding": "interpret_data_coverage",
    "execute_capabilities": "reduce_evidence",
    "reduce_evidence": "decide_next_action",
    "promotion_direction": "promotion_policy_gate",
    "execute_joint_attribution": "reduce_evidence",
    "interpret_evidence": "audit_causal_implications",
    "audit_causal_implications": "synthesize_answer",
    "synthesize_answer": "semantic_audit",
    "sanitize_answer": "hard_verify_answer",
    "repair_answer": "semantic_audit",
    "generate_degraded_explanation": "final_business_summary",
    "generate_blocked_explanation": "final_business_summary",
    "final_business_summary": "persist_artifact",
}

CONDITIONAL_NEXT: dict[str, tuple[Callable[[dict[str, Any]], str], dict[str, str]]] = {
    "clarification_policy_gate": (
        workflow._route_after_clarification_policy,
        {
            "confirm": "confirm_business_understanding",
            "ask": "generate_clarification",
            "block": "generate_blocked_explanation",
        },
    ),
    "generate_clarification": (
        workflow._route_after_clarification,
        {"rebind": "rebind_after_clarification", "block": "generate_blocked_explanation"},
    ),
    "accept_analysis_route": (
        workflow._route_after_accept_analysis,
        {
            "accepted": "inspect_schema",
            "repair": "repair_analysis_route",
            "ask": "generate_clarification",
            "degrade": "generate_degraded_explanation",
            "block": "generate_blocked_explanation",
        },
    ),
    "interpret_data_coverage": (
        workflow._route_after_coverage,
        {
            "sufficient": "execute_capabilities",
            "ask": "generate_clarification",
            "degrade": "generate_degraded_explanation",
            "block": "generate_blocked_explanation",
        },
    ),
    "decide_next_action": (
        workflow._route_after_next_action,
        {
            "plan": "design_analysis_route",
            "ask": "generate_clarification",
            "promote": "promotion_direction",
            "synthesize": "interpret_evidence",
            "degrade": "generate_degraded_explanation",
        },
    ),
    "promotion_policy_gate": (
        workflow._route_after_promotion_policy,
        {
            "accepted": "execute_joint_attribution",
            "synthesize": "interpret_evidence",
            "ask": "generate_clarification",
            "degrade": "generate_degraded_explanation",
            "block": "generate_blocked_explanation",
        },
    ),
    "semantic_audit": (
        workflow._route_after_semantic_audit,
        {
            "verify": "hard_verify_answer",
            "repair": "repair_answer",
            "sanitize": "sanitize_answer",
            "degrade": "generate_degraded_explanation",
        },
    ),
    "hard_verify_answer": (
        workflow._route_after_hard_verify,
        {
            "passed": "final_business_summary",
            "repair": "repair_answer",
            "ask": "generate_clarification",
            "degrade": "generate_degraded_explanation",
        },
    ),
}

LLM_NODES = {
    "understand_business_intent",
    "decide_question_boundary",
    "generate_clarification",
    "confirm_business_understanding",
    "design_analysis_route",
    "repair_analysis_route",
    "interpret_data_coverage",
    "decide_next_action",
    "promotion_direction",
    "interpret_evidence",
    "audit_causal_implications",
    "synthesize_answer",
    "semantic_audit",
    "repair_answer",
    "generate_degraded_explanation",
    "generate_blocked_explanation",
    "final_business_summary",
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Phase 5 system tests with real ClickHouse, real LLM, and node-by-node audit."
    )
    parser.add_argument("--case-file", default=str(DEFAULT_CASE_FILE))
    parser.add_argument("--artifact-root", default="")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--max-nodes", type=int, default=80)
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args(argv)

    case_file = Path(args.case_file)
    artifact_root = Path(args.artifact_root) if args.artifact_root else _default_artifact_root()
    selected = set(args.case)
    cases = _load_live_cases(case_file)
    if selected:
        cases = [case for case in cases if case["case_id"] in selected]
    if not cases:
        print(json.dumps({"status": "failed", "reason": "no_cases_selected"}, ensure_ascii=False))
        return 1

    results = []
    for case in cases:
        print(json.dumps({"event": "case_start", "case_id": case["case_id"]}, ensure_ascii=False), flush=True)
        result = run_case(case, artifact_root=artifact_root, max_nodes=args.max_nodes)
        results.append(result)
        print(json.dumps({"event": "case_done", **_case_line(result)}, ensure_ascii=False), flush=True)
        if args.fail_fast and result["status"] != "passed":
            break

    summary = {
        "status": "passed" if all(result["status"] == "passed" for result in results) else "failed",
        "artifact_root": str(artifact_root),
        "cases": results,
    }
    summary_path = artifact_root / "live_node_system_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(to_jsonable(summary), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"event": "summary", "status": summary["status"], "summary_path": str(summary_path)}, ensure_ascii=False), flush=True)
    return 0 if summary["status"] == "passed" else 1


def run_case(case: Mapping[str, Any], *, artifact_root: Path, max_nodes: int) -> dict[str, Any]:
    if case.get("fixture_rows"):
        return {
            "case_id": case["case_id"],
            "status": "failed",
            "reason": "fixture_rows_not_allowed_in_live_system_test",
            "nodes": [],
        }
    rows, sql_text = node_runner._rows_for_case(case)
    state = node_runner.build_initial_state(
        case,
        rows=rows,
        artifact_root=str(artifact_root),
        sql_text=sql_text,
    )
    case_dir = artifact_root / case["case_id"]
    state_path = case_dir / "state.json"
    _write_json(state_path, state)

    nodes = []
    node = "understand_business_intent"
    status = "passed"
    reason = ""
    for _ in range(max_nodes):
        print(json.dumps({"event": "node_start", "case_id": case["case_id"], "node": node}, ensure_ascii=False), flush=True)
        state = node_runner.run_one_node(state, node)
        _write_json(state_path, state)
        review = state["node_debug_reviews"][-1]
        node_result = _node_result(review, state)
        nodes.append(node_result)
        print(json.dumps({"event": "node_done", "case_id": case["case_id"], **node_result}, ensure_ascii=False), flush=True)
        if review["status"] != "completed":
            status = "failed"
            reason = review.get("error") or "node_failed"
            break
        if node == "persist_artifact":
            break
        next_node, state = _next_node(node, state)
        _write_json(state_path, state)
        if not next_node:
            status = "failed"
            reason = f"missing_next_node_after:{node}"
            break
        node = next_node
    else:
        status = "failed"
        reason = "max_nodes_exceeded"

    checks = _validate_live_case(case, state, nodes)
    if checks:
        status = "failed"
        reason = ";".join(checks)

    return {
        "case_id": case["case_id"],
        "status": status,
        "reason": reason,
        "state_path": str(state_path),
        "artifact_path": state.get("artifact_path", ""),
        "node_count": len(nodes),
        "llm_call_count": len(state.get("llm_calls", [])),
        "accepted_graph": _accepted_graph(state),
        "next_action": state.get("next_action", {}),
        "final_summary": state.get("final_business_summary", ""),
        "nodes": nodes,
    }


def _next_node(node: str, state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if node in FIXED_NEXT:
        return FIXED_NEXT[node], state
    route_config = CONDITIONAL_NEXT.get(node)
    if not route_config:
        return "", state
    route_fn, mapping = route_config
    hydrated = node_runner._hydrate_state(state)
    route = route_fn(hydrated)
    updated = node_runner._strip_runtime_objects(hydrated)
    return mapping.get(route, ""), updated


def _validate_live_case(
    case: Mapping[str, Any],
    state: Mapping[str, Any],
    nodes: Sequence[Mapping[str, Any]],
) -> list[str]:
    issues = []
    llm_calls = list(state.get("llm_calls", []))
    if not llm_calls:
        issues.append("missing_llm_calls")
    for call in llm_calls:
        if call.get("provider") == "fake" or call.get("model") == "fake-model":
            issues.append("fake_llm_call_detected")
        for key in ("messages", "raw_response_content", "started_at", "finished_at", "duration_ms"):
            if key not in call:
                issues.append(f"llm_audit_missing:{key}")
    for node in nodes:
        if node["node"] in LLM_NODES and not node.get("llm_tasks_added"):
            issues.append(f"llm_node_without_llm_call:{node['node']}")
    summary = str(state.get("final_business_summary") or "")
    for token in ("all_users", "monthly_daily_avg", "方向命中率", "重要性命中率", "单用户/单订单"):
        if token in summary:
            issues.append(f"visible_text_leaks:{token}")
    if not state.get("artifact_path"):
        issues.append("missing_answer_package")
    boundary_status = str(state.get("clarification_outcome", {}).get("boundary_status") or "")
    if boundary_status in set(case.get("forbidden_boundary_statuses", [])):
        issues.append(f"forbidden_boundary_status:{boundary_status}")
    final_status = str((state.get("final_explanation") or {}).get("status") or "")
    if final_status in set(case.get("forbidden_final_statuses", [])):
        issues.append(f"forbidden_final_status:{final_status}")
    allowed_final = set(case.get("allowed_final_statuses", ()))
    if allowed_final:
        observed_final = final_status or "passed"
        if observed_final not in allowed_final:
            issues.append(f"unexpected_final_status:{observed_final}")
    accepted = set(_accepted_graph(state))
    for capability in case.get("required_accepted_capabilities", []):
        if capability not in accepted:
            issues.append(f"missing_required_capability:{capability}")
    return sorted(set(issues))


def _node_result(review: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "node": review.get("node", ""),
        "status": review.get("status", ""),
        "duration_ms": review.get("duration_ms", 0),
        "llm_tasks_added": list(review.get("llm_tasks_added", [])),
        "changed_keys": list(review.get("changed_keys", [])),
        "error": review.get("error", ""),
        "route": (review.get("checkpoint") or {}).get("route", ""),
        "llm_call_count": len(state.get("llm_calls", [])),
    }


def _accepted_graph(state: Mapping[str, Any]) -> list[str]:
    compiled = state.get("compiled_graph") or {}
    mutations = compiled.get("mutations", {}) if isinstance(compiled, Mapping) else {}
    return list(mutations.get("accepted_graph", []))


def _load_live_cases(path: Path) -> list[dict[str, Any]]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    source_files = list(loaded.get("source_case_files") or ())
    if loaded.get("source_case_file"):
        source_files.append(str(loaded["source_case_file"]))
    if not source_files:
        source_files.append("evals/phase4/full_period_pattern_cases.yaml")
    source_cases = {}
    for source_file in source_files:
        for source_case in node_runner._load_cases(ROOT / str(source_file)):
            existing = source_cases.get(source_case["case_id"])
            if existing and existing.get("real_sql") and not source_case.get("real_sql"):
                continue
            source_cases[source_case["case_id"]] = source_case
    cases = []
    for item in loaded.get("cases", []):
        source_id = item.get("source_case_id")
        if source_id:
            case = dict(source_cases[source_id])
            case.update({key: value for key, value in item.items() if key != "source_case_id"})
        else:
            case = dict(item)
        cases.append(case)
    return cases


def _case_line(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "case_id": result["case_id"],
        "status": result["status"],
        "reason": result.get("reason", ""),
        "node_count": result.get("node_count", 0),
        "llm_call_count": result.get("llm_call_count", 0),
        "artifact_path": result.get("artifact_path", ""),
    }


def _default_artifact_root() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return DEFAULT_ARTIFACT_ROOT / stamp


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
