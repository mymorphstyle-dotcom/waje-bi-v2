from __future__ import annotations

# This executable bootstraps the repository root before importing project modules.
# ruff: noqa: E402

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.langgraph_workflow import _understand_business_intent
from bi_agent.runtime.mainland_model_provider import MainlandModelProvider


QUESTION = "2026年6月1日付费金额为什么上涨？主要由哪些指标变化导致？"
DEFAULT_ROOT = Path("artifacts/phase7/single-authority-phase01")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    stability = subparsers.add_parser("intent-stability")
    stability.add_argument("--count", type=int, default=10)
    stability.add_argument("--artifact-root", type=Path, default=DEFAULT_ROOT)
    case_b = subparsers.add_parser("case-b")
    case_b.add_argument("--artifact-root", type=Path, default=DEFAULT_ROOT)
    reassess = subparsers.add_parser("intent-stability-reassess")
    reassess.add_argument("--artifact-directory", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "intent-stability":
        return run_intent_stability(args.count, args.artifact_root)
    if args.command == "intent-stability-reassess":
        return reassess_intent_stability(args.artifact_directory)
    return run_case_b(args.artifact_root)


def run_intent_stability(count: int, artifact_root: Path) -> int:
    if count != 10:
        raise ValueError("intent_stability_requires_exactly_ten_calls")
    acceptance_id = _acceptance_id("intent-stability")
    output_dir = _new_directory(artifact_root / acceptance_id)
    store = PostgresConversationStore.from_env()
    store.apply_schema()
    client = MainlandModelProvider.structured_client_from_env()
    material_views: list[dict[str, Any]] = []
    try:
        for index in range(1, count + 1):
            run_id = f"phase01-live-intent-{uuid4().hex}"
            thread_id = f"phase01-live-thread-{uuid4().hex}"
            store.create_thread(thread_id, owner_id="phase01-live-acceptance")
            store.upsert_run(run_id, thread_id=thread_id, status="running")
            state: dict[str, Any] = {
                "run_id": run_id,
                "request": {
                    "question": QUESTION,
                    "run_attempt_id": run_id,
                    "authority_store": store,
                },
                "llm_client": client,
                "llm_calls": [],
                "checkpoint_events": [],
                "validator_results": [],
            }
            try:
                output = _understand_business_intent(state)
            except Exception as exc:
                failure = {
                    "call_index": index,
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "failure_type": type(exc).__name__,
                    "failure_code": str(exc),
                    "llm_calls": list(state.get("llm_calls") or ()),
                }
                _write_new_json(
                    output_dir / f"intent-call-{index:02d}-failed.json",
                    failure,
                )
                raise
            revision = dict(output["intent_revision"])
            audit = next(
                item
                for item in reversed(output["llm_calls"])
                if item.get("task") == "single_authority_intent"
            )
            raw = dict(output["raw_intent_output"])
            record = {
                "call_index": index,
                "run_id": run_id,
                "thread_id": thread_id,
                "prompt_version": audit.get("prompt_version"),
                "provider": audit.get("provider"),
                "model": audit.get("model"),
                "response_id": audit.get("response_id"),
                "attempt_count": audit.get("attempt_count"),
                "raw_structured_output": raw,
                "raw_response_content": audit.get("raw_response_content"),
                "intent_revision": revision,
                "durable_checkpoint": output["durable_checkpoint"],
            }
            _write_new_json(output_dir / f"intent-call-{index:02d}.json", record)
            material_views.append(_stability_view(revision))
        baseline = material_views[0]
        comparisons = [
            {
                "call_index": index,
                "matches_first": view == baseline,
                "view": view,
            }
            for index, view in enumerate(material_views, start=1)
        ]
        report = {
            "acceptance_id": acceptance_id,
            "question": QUESTION,
            "call_count": count,
            "all_required_bindings_stable": all(
                item["matches_first"] for item in comparisons
            ),
            "required_stability_fields": list(baseline),
            "comparisons": comparisons,
            "artifact_directory": str(output_dir.resolve()),
        }
        _write_new_json(output_dir / "stability-report.json", report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["all_required_bindings_stable"] else 2
    finally:
        store.connection.close()


def run_case_b(artifact_root: Path) -> int:
    acceptance_id = _acceptance_id("case-b")
    output_dir = _new_directory(artifact_root / acceptance_id)
    core = ConversationAgentCore.from_environment()
    run_id = f"phase01-live-case-b-{uuid4().hex}"
    thread_id = f"phase01-live-case-b-thread-{uuid4().hex}"
    owner_id = "phase01-live-acceptance"
    core.store.create_thread(thread_id, owner_id=owner_id)
    try:
        result = core.run_message(
            thread_id=thread_id,
            run_id=run_id,
            user_message=QUESTION,
            user_id=owner_id,
            artifact_root=str(output_dir),
        )
        record = {
            "acceptance_id": acceptance_id,
            "question": QUESTION,
            "run_id": run_id,
            "thread_id": thread_id,
            "result": result,
            "artifact_directory": str(output_dir.resolve()),
        }
        _write_new_json(output_dir / "case-b-baseline-clarification.json", record)
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("status") == "waiting_for_clarification" else 3
    finally:
        core.store.connection.close()


def reassess_intent_stability(output_dir: Path) -> int:
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(output_dir.glob("intent-call-[0-9][0-9].json"))
    ]
    if len(records) != 10:
        raise ValueError("intent_stability_reassessment_requires_ten_records")
    views = [_stability_view(record["intent_revision"]) for record in records]
    baseline = views[0]
    comparisons = [
        {
            "call_index": index,
            "matches_first": view == baseline,
            "view": view,
        }
        for index, view in enumerate(views, start=1)
    ]
    report = {
        "acceptance_id": output_dir.name,
        "question": QUESTION,
        "call_count": len(records),
        "all_required_bindings_stable": all(
            item["matches_first"] for item in comparisons
        ),
        "required_stability_fields": list(baseline),
        "display_fields_excluded": ["ambiguity_slots[].question"],
        "comparisons": comparisons,
        "artifact_directory": str(output_dir.resolve()),
    }
    _write_new_json(output_dir / "stability-material-report.json", report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["all_required_bindings_stable"] else 2


def _stability_view(revision: Mapping[str, Any]) -> dict[str, Any]:
    ambiguity_slots = [
        {
            key: slot.get(key)
            for key in (
                "slot_id",
                "slot_kind",
                "materiality",
                "status",
                "allowed_value_refs",
            )
        }
        for slot in revision.get("ambiguity_slots") or ()
        if isinstance(slot, Mapping)
    ]
    return {
        "goal_bindings": revision.get("goal_bindings"),
        "target_metric_refs": revision.get("target_metric_refs"),
        "scope": revision.get("scope"),
        "time_spec": revision.get("time_spec"),
        "direction_premise": revision.get("direction_premise"),
        "ambiguity_slots": ambiguity_slots,
        "desired_decisions": revision.get("desired_decisions"),
    }


def _acceptance_id(label: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{label}-{stamp}-{uuid4().hex[:10]}"


def _new_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=False)
    return path


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
