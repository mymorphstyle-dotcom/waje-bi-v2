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
from bi_agent.runtime.authoritative_plan_result import (
    ParsedAuthoritativePlanResult,
    parse_authoritative_plan_result,
)
from bi_agent.runtime.evidence_authority import (
    canonical_digest,
    canonical_value,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.runtime.single_authority import (
    DurableTransition,
    IntentRevision,
)


QUESTION = "2026年6月1日付费金额为什么上涨？主要由哪些指标变化导致？"
OWNER_ID = "phase02-live-acceptance"
DEFAULT_ROOT = Path("artifacts/phase7/single-authority-phase02")
REQUIRED_CASE_B_AXES = frozenset(
    {
        "change_validation",
        "formula_tree",
        "dimension_localization",
        "time_context",
        "data_quality",
    }
)
REQUIRED_CASE_B_CAPABILITIES = frozenset(
    {
        "compare_periods",
        "formula_decompose",
        "candidate_dimension_screen",
        "metric_timeseries",
        "data_quality_profile",
    }
)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("case-b-start")
    start.add_argument("--artifact-root", type=Path, default=DEFAULT_ROOT)
    plan = subparsers.add_parser("case-b-plan")
    plan.add_argument("--artifact-directory", type=Path, required=True)
    plan.add_argument("--option-id", required=True)
    reassess = subparsers.add_parser("reassess")
    reassess.add_argument("--artifact-directory", type=Path, required=True)
    reassess_start = subparsers.add_parser("reassess-start")
    reassess_start.add_argument("--artifact-directory", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "case-b-start":
        return run_case_b_start(args.artifact_root)
    if args.command == "case-b-plan":
        return run_case_b_plan(
            args.artifact_directory,
            option_id=args.option_id,
        )
    if args.command == "reassess-start":
        return reassess_case_b_start(args.artifact_directory)
    return reassess_case_b_plan(args.artifact_directory)


def run_case_b_start(artifact_root: Path) -> int:
    acceptance_id = _acceptance_id("case-b")
    output_dir = _new_directory(artifact_root / acceptance_id)
    core = ConversationAgentCore.from_environment()
    run_id = f"phase02-live-case-b-{uuid4().hex}"
    thread_id = f"phase02-live-case-b-thread-{uuid4().hex}"
    try:
        core.store.apply_schema()
        core.store.create_thread(thread_id, owner_id=OWNER_ID)
        result = core.run_message(
            thread_id=thread_id,
            run_id=run_id,
            user_message=QUESTION,
            user_id=OWNER_ID,
            artifact_root=str(output_dir),
        )
        review = _review_start_result(result, run_id=run_id)
        review["persistence"] = _review_start_persistence(core.store, result)
        review["passed"] = bool(review["passed"] and review["persistence"]["passed"])
        record = {
            "schema_version": "single-authority-phase02-acceptance-start.v1",
            "acceptance_id": acceptance_id,
            "question": QUESTION,
            "owner_id": OWNER_ID,
            "run_id": run_id,
            "thread_id": thread_id,
            "result": result,
            "review": review,
            "artifact_directory": str(output_dir.resolve()),
        }
        _write_new_json(output_dir / "case-b-start.json", record)
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        return 0 if review["passed"] else 3
    finally:
        core.store.connection.close()


def reassess_case_b_start(output_dir: Path) -> int:
    output_dir = output_dir.resolve()
    start = _load_start_record(output_dir)
    result = start.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("phase02_acceptance_start_result_missing")
    store = PostgresConversationStore.from_env()
    try:
        persistence = _review_start_persistence(store, result)
    finally:
        store.connection.close()
    review = _review_start_result(
        result,
        run_id=str(start.get("run_id") or ""),
    )
    review["persistence"] = persistence
    review["passed"] = bool(review["passed"] and persistence["passed"])
    report = {
        "schema_version": "single-authority-phase02-start-reassessment.v1",
        "acceptance_id": start.get("acceptance_id"),
        "run_id": start.get("run_id"),
        "review": review,
        "artifact_directory": str(output_dir),
    }
    _write_new_json(output_dir / "case-b-start-reassessment.json", report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if review["passed"] else 6


def run_case_b_plan(
    output_dir: Path,
    *,
    option_id: str,
) -> int:
    output_dir = output_dir.resolve()
    plan_path = output_dir / "case-b-plan.json"
    if plan_path.exists():
        raise FileExistsError(plan_path)
    start = _load_start_record(output_dir)
    option = _selected_option(start, option_id)
    run_id = str(start["run_id"])
    thread_id = str(start["thread_id"])
    owner_id = str(start["owner_id"])
    answer = str(option["label"])
    core = ConversationAgentCore.from_environment()
    try:
        core.store.apply_schema()
        persisted_artifact_root = _resume_artifact_root(
            core.store,
            run_id,
            output_dir,
        )
        result = core.run_message(
            thread_id=thread_id,
            run_id=run_id,
            user_message=answer,
            user_id=owner_id,
            artifact_root=persisted_artifact_root,
            clarification={
                "sourceRunId": run_id,
                "resolutionId": (f"phase02-acceptance:{run_id}:{option_id}"),
                "attemptRunId": run_id,
                "answer": answer,
                "selectedOptionId": option_id,
                "source": "user",
                "retryAttempt": False,
            },
            stop_after_phase="phase02",
        )
        if result.get("status") == "planned":
            llm_calls = result.get("llm_calls")
            if not isinstance(llm_calls, list):
                raise ValueError("phase02_acceptance_llm_audit_missing")
            parsed = parse_authoritative_plan_result(
                result.get("plan_result"),
                expected_run_id=run_id,
                expected_llm_calls=llm_calls,
            )
            review = _review_planned_result(parsed)
            review["persistence"] = _review_persistence(core.store, parsed)
            review["passed"] = bool(
                review["passed"] and review["persistence"]["passed"]
            )
        else:
            review = {
                "schema_version": "single-authority-phase02-acceptance-review.v1",
                "passed": False,
                "failure": "planned_status_required",
                "observed_status": result.get("status"),
            }
        record = {
            "schema_version": "single-authority-phase02-acceptance-plan.v1",
            "acceptance_id": start["acceptance_id"],
            "question": start["question"],
            "run_id": run_id,
            "thread_id": thread_id,
            "selected_option_id": option_id,
            "result": result,
            "review": review,
            "artifact_directory": str(output_dir),
        }
        _write_new_json(plan_path, record)
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        return 0 if review["passed"] else 4
    finally:
        core.store.connection.close()


def reassess_case_b_plan(output_dir: Path) -> int:
    output_dir = output_dir.resolve()
    record = _load_json(output_dir / "case-b-plan.json")
    if record.get("schema_version") != ("single-authority-phase02-acceptance-plan.v1"):
        raise ValueError("phase02_acceptance_plan_record_invalid")
    result = record.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("phase02_acceptance_plan_result_missing")
    llm_calls = result.get("llm_calls")
    if not isinstance(llm_calls, list):
        raise ValueError("phase02_acceptance_llm_audit_missing")
    parsed = parse_authoritative_plan_result(
        result.get("plan_result"),
        expected_run_id=str(record.get("run_id") or ""),
        expected_llm_calls=llm_calls,
    )
    review = _review_planned_result(parsed)
    report = {
        "schema_version": "single-authority-phase02-reassessment.v1",
        "acceptance_id": record.get("acceptance_id"),
        "run_id": record.get("run_id"),
        "review": review,
        "artifact_directory": str(output_dir),
    }
    _write_new_json(output_dir / "case-b-reassessment.json", report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if review["passed"] else 5


def _review_start_result(
    result: Mapping[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    clarification = result.get("clarification")
    options = (
        clarification.get("options") if isinstance(clarification, Mapping) else None
    )
    valid_options = (
        isinstance(options, list)
        and 3 <= len(options) <= 4
        and all(isinstance(item, Mapping) for item in options)
    )
    option_ids = [
        str(item.get("option_id") or "")
        for item in options or ()
        if isinstance(item, Mapping)
    ]
    recommended_ids = [
        str(item.get("option_id") or "")
        for item in options or ()
        if isinstance(item, Mapping) and item.get("recommended") is True
    ]
    business_option_ids = [
        option_id for option_id in option_ids if option_id != "tell_agent_differently"
    ]
    passed = bool(
        result.get("status") == "waiting_for_clarification"
        and result.get("run_id") == run_id
        and isinstance(result.get("intent_revision"), Mapping)
        and isinstance(result.get("decision_ledger"), Mapping)
        and isinstance(result.get("durable_checkpoint"), Mapping)
        and valid_options
        and all(option_ids)
        and len(option_ids) == len(set(option_ids))
        and 2 <= len(business_option_ids) <= 3
        and option_ids[-1] == "tell_agent_differently"
        and recommended_ids == ["comparison_baseline.previous_day"]
    )
    return {
        "schema_version": "single-authority-phase02-start-review.v1",
        "passed": passed,
        "status": result.get("status"),
        "option_ids": option_ids,
        "recommended_option_ids": recommended_ids,
        "human_selection_required": True,
    }


def _review_start_persistence(
    store: Any,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        intent = IntentRevision.from_dict(result["intent_revision"])
        transition = DurableTransition.from_dict(result["durable_checkpoint"])
        ledger_payload = result["decision_ledger"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("phase02_acceptance_start_authority_invalid") from exc
    if (
        not isinstance(ledger_payload, Mapping)
        or set(ledger_payload) != {"position", "records"}
        or isinstance(ledger_payload.get("position"), bool)
        or not isinstance(ledger_payload.get("position"), int)
        or not isinstance(ledger_payload.get("records"), list)
    ):
        raise ValueError("phase02_acceptance_start_ledger_invalid")
    persisted_intent = store.resolve_active_intent_revision(intent.run_attempt_id)
    persisted_ledger = store.load_decision_ledger(intent.intent_revision_id)
    persisted_transition = store.load_accepted_transition(
        run_attempt_id=intent.run_attempt_id,
        node_name=transition.node_name,
        input_digest=transition.input_digest,
    )
    transition_input = (
        persisted_transition.get("input_payload")
        if isinstance(persisted_transition, Mapping)
        else None
    )
    transition_output = (
        persisted_transition.get("output_payload")
        if isinstance(persisted_transition, Mapping)
        else None
    )
    persisted_records = [record.to_dict() for record in persisted_ledger.records]
    passed = bool(
        result.get("run_id") == intent.run_attempt_id
        and persisted_intent == intent
        and persisted_ledger.position == ledger_payload["position"]
        and canonical_value(persisted_records)
        == canonical_value(ledger_payload["records"])
        and transition.run_attempt_id == intent.run_attempt_id
        and transition.intent_revision_id == intent.intent_revision_id
        and transition.decision_ledger_position == persisted_ledger.position
        and transition.node_name == "persist_waiting_for_decision"
        and transition.status == "succeeded"
        and transition.acceptance_state == "accepted"
        and transition.next_transition == "await_user_decision"
        and store.latest_accepted_transition_id(intent.run_attempt_id)
        == transition.transition_id
        and isinstance(persisted_transition, Mapping)
        and persisted_transition.get("transition") == transition
        and isinstance(transition_input, Mapping)
        and isinstance(transition_output, Mapping)
        and canonical_digest(transition_input) == transition.input_digest
        and canonical_digest(transition_output) == transition.output_digest
    )
    return {
        "passed": passed,
        "intent_revision_id": intent.intent_revision_id,
        "decision_ledger_position": persisted_ledger.position,
        "accepted_transition_id": transition.transition_id,
    }


def _review_planned_result(
    parsed: ParsedAuthoritativePlanResult,
) -> dict[str, Any]:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    context = parsed.authority_context
    plan = parsed.plan_revision
    if (
        context.contract_versions.get("runtime_bindings") != registry.contract_version
        or context.contract_versions.get("runtime_bindings_digest")
        != registry.source_payload_digest
    ):
        raise ValueError("phase02_acceptance_runtime_contract_drift")

    axis_ids = {axis.axis_id for axis in plan.analysis_axes}
    capability_ids = {task.capability_id for task in plan.capability_tasks}
    coverage = {
        str(item["dataset_id"]): canonical_value(item)
        for item in context.dataset_coverage
    }
    task_boundaries: list[dict[str, Any]] = []
    input_state_consistent = True
    for task in plan.capability_tasks:
        for state in task.execution_policy["input_states"]:
            dataset_id = str(state["input_ref"]).removeprefix("dataset:")
            authority = coverage.get(dataset_id)
            if authority is None or (
                state["availability"] != authority["availability"]
                or state["limitation_ref"] != authority["limitation_ref"]
            ):
                input_state_consistent = False
            if state["availability"] != "claim_ready":
                task_boundaries.append(
                    {
                        "task_id": task.task_id,
                        "capability_id": task.capability_id,
                        "dataset_id": dataset_id,
                        "availability": state["availability"],
                        "limitation_ref": state["limitation_ref"],
                    }
                )
    payment_attempt = coverage.get("payment_attempt")
    payment_unavailable_is_bounded = bool(
        payment_attempt is None
        or payment_attempt["availability"] == "claim_ready"
        or any(item["dataset_id"] == "payment_attempt" for item in task_boundaries)
    )
    admission_counts = {
        status: sum(
            entry["status"] == status
            for entry in parsed.proposal_admission.admission_entries
        )
        for status in ("admitted", "rejected", "deferred")
    }
    release_refs = list(context.release_refs)
    snapshot_refs = list(context.snapshot_refs)
    passed = bool(
        REQUIRED_CASE_B_AXES <= axis_ids
        and REQUIRED_CASE_B_CAPABILITIES <= capability_ids
        and input_state_consistent
        and payment_unavailable_is_bounded
        and len(release_refs) == len(set(release_refs))
        and len(snapshot_refs) == len(set(snapshot_refs))
    )
    return {
        "schema_version": "single-authority-phase02-acceptance-review.v1",
        "passed": passed,
        "plan_revision_id": plan.plan_revision_id,
        "plan_digest": plan.content_digest,
        "planner_proposal_id": parsed.planner_proposal.planner_proposal_id,
        "planner_proposal_digest": parsed.planner_proposal.content_digest,
        "proposal_admission_id": (parsed.proposal_admission.proposal_admission_id),
        "proposal_admission_digest": (parsed.proposal_admission.content_digest),
        "accepted_transition_id": parsed.transition.transition_id,
        "axis_ids": sorted(axis_ids),
        "capability_ids": sorted(capability_ids),
        "admission_counts": admission_counts,
        "release_refs": release_refs,
        "snapshot_refs": snapshot_refs,
        "task_boundaries": task_boundaries,
        "all_task_inputs_use_pinned_authority_context": (input_state_consistent),
        "payment_attempt_unavailability_is_plan_bounded": (
            payment_unavailable_is_bounded
        ),
    }


def _review_persistence(
    store: Any,
    parsed: ParsedAuthoritativePlanResult,
) -> dict[str, Any]:
    plan = parsed.plan_revision
    persisted_transition = store.load_accepted_transition(
        run_attempt_id=plan.run_attempt_id,
        node_name="compile_authoritative_plan",
        input_digest=parsed.transition.input_digest,
    )
    passed = bool(
        store.resolve_active_plan_revision(plan.run_attempt_id) == plan
        and store.load_authority_context(plan.run_attempt_id)
        == parsed.authority_context
        and store.load_planner_proposal(parsed.planner_proposal.planner_proposal_id)
        == parsed.planner_proposal
        and store.load_proposal_admission(
            parsed.proposal_admission.proposal_admission_id
        )
        == parsed.proposal_admission
        and isinstance(persisted_transition, Mapping)
        and persisted_transition.get("transition") == parsed.transition
        and canonical_value(persisted_transition.get("input_payload"))
        == canonical_value(parsed.transition_input)
        and canonical_value(persisted_transition.get("output_payload"))
        == canonical_value(parsed.transition_output)
    )
    return {
        "passed": passed,
        "one_active_plan_digest": plan.content_digest,
        "accepted_transition_id": parsed.transition.transition_id,
    }


def _load_start_record(output_dir: Path) -> dict[str, Any]:
    record = _load_json(output_dir / "case-b-start.json")
    if (
        record.get("schema_version") != "single-authority-phase02-acceptance-start.v1"
        or record.get("artifact_directory") != str(output_dir)
        or record.get("question") != QUESTION
        or record.get("owner_id") != OWNER_ID
    ):
        raise ValueError("phase02_acceptance_start_record_invalid")
    return record


def _selected_option(
    start: Mapping[str, Any],
    option_id: str,
) -> Mapping[str, Any]:
    if (
        not option_id
        or option_id != option_id.strip()
        or option_id == "tell_agent_differently"
    ):
        raise ValueError("phase02_acceptance_option_id_invalid")
    result = start.get("result")
    clarification = result.get("clarification") if isinstance(result, Mapping) else None
    options = (
        clarification.get("options") if isinstance(clarification, Mapping) else None
    )
    matches = [
        item
        for item in options or ()
        if isinstance(item, Mapping) and item.get("option_id") == option_id
    ]
    if len(matches) != 1 or not str(matches[0].get("label") or "").strip():
        raise ValueError("phase02_acceptance_option_id_unknown")
    return matches[0]


def _resume_artifact_root(
    store: Any,
    run_id: str,
    expected_directory: Path,
) -> str:
    run_state = store.get_run_state(run_id)
    request = run_state.get("request") if isinstance(run_state, Mapping) else None
    runtime_descriptors = (
        request.get("runtime_descriptors") if isinstance(request, Mapping) else None
    )
    artifact_root = (
        runtime_descriptors.get("artifact_root")
        if isinstance(runtime_descriptors, Mapping)
        else None
    )
    if (
        not isinstance(artifact_root, str)
        or not artifact_root
        or Path(artifact_root).resolve() != expected_directory.resolve()
    ):
        raise ValueError("phase02_acceptance_artifact_root_mismatch")
    return artifact_root


def _acceptance_id(label: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{label}-{stamp}-{uuid4().hex[:10]}"


def _new_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=False)
    return path


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("phase02_acceptance_artifact_invalid")
    return payload


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
