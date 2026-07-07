#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from typing import Any, Mapping, Optional, Sequence

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bi_agent.runtime import langgraph_workflow as workflow
from bi_agent.runtime.artifacts import to_jsonable
from bi_agent.runtime.capability_models import BudgetState
from bi_agent.runtime.clickhouse_runtime import ClickHouseRuntime, ENV_NAMES
from bi_agent.runtime.llm_client import OpenAICompatibleLLMClient
from bi_agent.runtime.models import CompiledGraph, GraphNode, MutationLedger, MutationRecord
from bi_agent.runtime.sql_safety import validate_select_only
from tools.phase4.validate_phase4 import _load_local_env, _required_fields_for_case


DEFAULT_CASE_FILE = ROOT / "evals" / "phase4" / "full_period_pattern_cases.yaml"
DEFAULT_ARTIFACT_ROOT = ROOT / "artifacts" / "phase-5" / "node-debug"

NODE_FUNCS = {
    "understand_business_intent": workflow._understand_business_intent,
    "decide_question_boundary": workflow._decide_question_boundary,
    "clarification_policy_gate": workflow._clarification_policy_gate,
    "generate_clarification": workflow._generate_clarification,
    "rebind_after_clarification": workflow._rebind_after_clarification,
    "confirm_business_understanding": workflow._confirm_business_understanding,
    "design_analysis_route": workflow._design_analysis_route,
    "accept_analysis_route": workflow._accept_analysis_route,
    "repair_analysis_route": workflow._repair_analysis_route,
    "inspect_schema": workflow._inspect_schema,
    "validate_runtime_binding": workflow._validate_runtime_binding,
    "interpret_data_coverage": workflow._interpret_data_coverage,
    "execute_capabilities": workflow._execute_capabilities,
    "reduce_evidence": workflow._reduce_evidence,
    "decide_next_action": workflow._decide_next_action,
    "promotion_direction": workflow._promotion_direction,
    "promotion_policy_gate": workflow._promotion_policy_gate,
    "execute_joint_attribution": workflow._execute_joint_attribution,
    "interpret_evidence": workflow._interpret_evidence,
    "audit_causal_implications": workflow._audit_causal_implications,
    "synthesize_answer": workflow._synthesize_answer,
    "semantic_audit": workflow._semantic_audit,
    "sanitize_answer": workflow._sanitize_answer,
    "hard_verify_answer": workflow._hard_verify_answer,
    "repair_answer": workflow._repair_answer,
    "generate_degraded_explanation": workflow._generate_degraded_explanation,
    "generate_blocked_explanation": workflow._generate_blocked_explanation,
    "final_business_summary": workflow._final_business_summary,
    "persist_artifact": workflow._persist_artifact,
}


def build_initial_state(
    case: Mapping[str, Any],
    *,
    rows: Sequence[Mapping[str, Any]],
    artifact_root: str,
    sql_text: str = "",
) -> dict[str, Any]:
    request = {
        "run_id": f"phase5-node-debug-{case['case_id']}",
        "artifact_root": artifact_root,
        "pattern_family": case["pattern_family"],
        "pattern_params": dict(case.get("pattern_params", {})),
        "time_window": case.get("time_window", ""),
        "rows": [dict(row) for row in rows],
        "required_fields": _required_fields_for_case(case),
        "requested_nodes": list(case.get("required_capabilities", ())),
        "allow_question_interrupt": True,
    }
    for key in (
        "question",
        "baseline",
        "target",
        "scope",
        "target_metric",
        "events",
        "segments",
        "primary_question_family",
        "secondary_question_families",
    ):
        if key in case:
            request[key] = case[key]
    if sql_text:
        request["sql_text"] = sql_text
    return {
        "request": request,
        "run_id": request["run_id"],
        "checkpoint_events": [],
        "validator_results": [],
        "llm_calls": [],
        "repair_attempts": 0,
        "answer_repair_attempts": 0,
        "node_debug_reviews": [],
    }


def run_one_node(
    state: Mapping[str, Any],
    node_name: str,
    *,
    llm_client: Any = None,
) -> dict[str, Any]:
    if node_name not in NODE_FUNCS:
        raise ValueError(f"unknown_node:{node_name}")
    live_state = _hydrate_state(state)
    live_state["llm_client"] = llm_client or _default_llm_client()
    before = _comparable_state(live_state)
    before_llm_count = len(live_state.get("llm_calls", []))
    started = perf_counter()

    try:
        workflow._retrying_node(node_name, NODE_FUNCS[node_name])(live_state)
        error = ""
    except Exception as exc:
        error = f"{type(exc).__name__}:{exc}"

    after = _strip_runtime_objects(live_state)
    after_comparable = _comparable_state(after)
    changed_keys = sorted(
        key
        for key in set(before) | set(after_comparable)
        if before.get(key) != after_comparable.get(key)
    )
    llm_tasks_added = [
        call.get("task")
        for call in after.get("llm_calls", [])[before_llm_count:]
        if call.get("task")
    ]
    review = {
        "node": node_name,
        "status": "failed" if error else "completed",
        "error": error,
        "duration_ms": round((perf_counter() - started) * 1000, 3),
        "changed_keys": changed_keys,
        "llm_tasks_added": llm_tasks_added,
        "checkpoint": after.get("checkpoint_events", [{}])[-1],
    }
    after.setdefault("node_debug_reviews", []).append(to_jsonable(review))
    if error:
        after["node_debug_error"] = error
    return to_jsonable(after)


def init_case_state(
    *,
    case_id: str,
    case_file: Path = DEFAULT_CASE_FILE,
    artifact_root: str = str(DEFAULT_ARTIFACT_ROOT),
) -> dict[str, Any]:
    case = _find_case(case_id, _load_cases(case_file))
    rows, sql_text = _rows_for_case(case)
    return build_initial_state(case, rows=rows, artifact_root=artifact_root, sql_text=sql_text)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Phase 5 workflow node at a time.")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-case")
    init.add_argument("--case-id", required=True)
    init.add_argument("--case-file", default=str(DEFAULT_CASE_FILE))
    init.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    init.add_argument("--state-path", required=True)
    run = sub.add_parser("run-node")
    run.add_argument("--state-path", required=True)
    run.add_argument("--node", required=True, choices=sorted(NODE_FUNCS))
    run.add_argument("--out-state-path", default="")
    args = parser.parse_args(argv)

    if args.command == "init-case":
        state = init_case_state(
            case_id=args.case_id,
            case_file=Path(args.case_file),
            artifact_root=args.artifact_root,
        )
        _write_json(Path(args.state_path), state)
        print(json.dumps({"state_path": args.state_path, "case_id": args.case_id}, ensure_ascii=False))
        return 0

    state_path = Path(args.state_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    updated = run_one_node(state, args.node)
    output_path = Path(args.out_state_path) if args.out_state_path else state_path
    _write_json(output_path, updated)
    print(json.dumps(updated["node_debug_reviews"][-1], ensure_ascii=False, indent=2))
    return 0


def _load_cases(path: Path) -> list[dict[str, Any]]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list(loaded.get("cases", ()))


def _find_case(case_id: str, cases: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    for case in cases:
        if case.get("case_id") == case_id:
            return case
    raise ValueError(f"unknown_case:{case_id}")


def _rows_for_case(case: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    if case.get("fixture_rows"):
        return [dict(row) for row in case["fixture_rows"]], str(case.get("real_sql") or "")
    sql = str(case.get("real_sql") or "")
    if not sql:
        return [], ""
    validation = validate_select_only(sql, aggregate=True)
    if not validation.ok:
        raise RuntimeError(f"invalid_case_sql:{validation.reason}")
    env = {**_load_local_env(ROOT / ".env"), **os.environ}
    missing = [name for name in ENV_NAMES if not env.get(name)]
    if missing:
        raise RuntimeError(f"missing_clickhouse_env:{','.join(missing)}")
    previous = {name: os.environ.get(name) for name in ENV_NAMES}
    try:
        os.environ.update({name: env[name] for name in ENV_NAMES})
        runtime = ClickHouseRuntime.from_env()
        if not runtime.configured():
            raise RuntimeError(f"invalid_clickhouse_binding:{runtime.binding.reason}")
        result = runtime.aggregate(sql, query_id=f"phase5-node-debug-{case['case_id']}")
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    if not result.ok:
        raise RuntimeError(f"clickhouse_query_failed:{result.reason}")
    return [dict(row) for row in result.rows], sql


def _default_llm_client() -> OpenAICompatibleLLMClient:
    return OpenAICompatibleLLMClient.from_env({**_load_local_env(ROOT / ".env"), **os.environ})


def _hydrate_state(state: Mapping[str, Any]) -> dict[str, Any]:
    hydrated = dict(state)
    if isinstance(hydrated.get("budget_state"), Mapping):
        hydrated["budget_state"] = BudgetState(**hydrated["budget_state"])
    if isinstance(hydrated.get("compiled_graph"), Mapping):
        hydrated["compiled_graph"] = _hydrate_compiled_graph(hydrated["compiled_graph"])
    return hydrated


def _hydrate_compiled_graph(value: Mapping[str, Any]) -> CompiledGraph:
    mutations = value.get("mutations", {})
    return CompiledGraph(
        status=value.get("status", ""),
        accepted_nodes=tuple(GraphNode(**node) for node in value.get("accepted_nodes", ())),
        mutations=MutationLedger(
            proposed_graph=tuple(mutations.get("proposed_graph", ())),
            accepted_graph=tuple(mutations.get("accepted_graph", ())),
            rejected_or_degraded=tuple(mutations.get("rejected_or_degraded", ())),
            records=tuple(MutationRecord(**record) for record in mutations.get("records", ())),
        ),
    )


def _strip_runtime_objects(state: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = {key: value for key, value in state.items() if key != "llm_client"}
    return to_jsonable(cleaned)


def _comparable_state(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: to_jsonable(value)
        for key, value in state.items()
        if key not in {"llm_client", "checkpoint_events", "llm_calls", "node_debug_reviews"}
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
