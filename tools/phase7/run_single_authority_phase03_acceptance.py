from __future__ import annotations

# ruff: noqa: E402

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.authoritative_execution_result import (
    AuthoritativeExecutionResult,
)
from bi_agent.runtime.capability_authority import (
    ExecutionSnapshot,
    ExplorationStopRecord,
)
from bi_agent.runtime.capability_scheduler import (
    capability_execution_transition_input,
    capability_execution_transition_payloads,
)
from bi_agent.runtime.claim_coverage import (
    ClaimCoverageCheckpoint,
    ClaimCoverageEvaluation,
    PlanExpansionDecision,
    PlanPatch,
    claim_coverage_transition_payloads,
    evaluate_claim_coverage,
)
from bi_agent.runtime.capability_task_adapter import (
    builtin_capability_adapter_registry,
)
from bi_agent.runtime.evidence_authority import (
    canonical_digest,
    canonical_value,
)
from bi_agent.runtime.single_authority import DurableTransition
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from tools.phase7.run_gateway_conversation_once import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    POST_EXECUTION_NON_PUBLICATION_OUTCOMES,
    POST_EXECUTION_NON_PUBLICATION_TERMINALS,
    _create_thread,
    _require_post_execution_state,
    _run_first_turn,
    _submit_clarification_resolution,
)
from tools.phase7 import run_live_conversation_system_test as terminal_acceptance


QUESTION = "2026年6月1日付费金额为什么上涨？主要由哪些指标变化导致？"
OWNER_ID = "phase03-live-acceptance"
DEFAULT_GATEWAY_BASE_URL = "http://127.0.0.1:3107"
DEFAULT_ROOT = Path("artifacts/phase7/single-authority-phase03")
PHASE01_ROOT = Path("artifacts/phase7/single-authority-phase01")
PHASE02_ROOT = Path("artifacts/phase7/single-authority-phase02")
PUBLIC_EXECUTION_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "result_ref",
        "plan_revision_id",
        "execution_snapshot_ref",
        "tasks",
        "outcomes",
        "obligations",
        "evidence",
        "failures",
        "limitations",
        "stop",
    }
)
PRIVATE_PROJECTION_MARKERS = (
    "raw_response",
    "raw_rows",
    "provider_ref",
    "model_ref",
    "technical_detail",
    "owner_id",
    "internal_owner",
    "debug",
    "observation_facts",
    "output_payload",
    "content_digest",
    "bundle_set_digest",
)
PRIVATE_KEY_FRAGMENTS = ("raw", "provider", "technical", "owner", "debug")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Phase 3 Case B acceptance through the live HTTP Gateway, "
            "then verify the persisted execution authority in Postgres."
        )
    )
    parser.add_argument("--env-file", default=".env")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("case-b-start")
    start.add_argument(
        "--gateway-base-url",
        default=DEFAULT_GATEWAY_BASE_URL,
    )
    start.add_argument("--artifact-root", type=Path, default=DEFAULT_ROOT)
    start.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    start.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
    )

    execute = subparsers.add_parser("case-b-execute")
    execute.add_argument("--artifact-directory", type=Path, required=True)
    execute.add_argument("--option-id", required=True)
    execute.add_argument("--gateway-base-url")
    execute.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    execute.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
    )

    reassess = subparsers.add_parser("reassess")
    reassess.add_argument("--artifact-directory", type=Path, required=True)
    args = parser.parse_args()
    terminal_acceptance.load_env_file(args.env_file)
    _validate_timing(args)
    if args.command == "case-b-start":
        return run_case_b_start(
            artifact_root=args.artifact_root,
            gateway_base_url=args.gateway_base_url,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
    if args.command == "case-b-execute":
        return run_case_b_execute(
            output_dir=args.artifact_directory,
            option_id=args.option_id,
            gateway_base_url=args.gateway_base_url,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
    return reassess_case_b_execution(args.artifact_directory)


def run_case_b_start(
    *,
    artifact_root: Path,
    gateway_base_url: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> int:
    root = _phase03_artifact_path(artifact_root)
    acceptance_id = _acceptance_id("case-b")
    output_dir = _new_directory(root / acceptance_id)
    gateway_base_url = _gateway_base_url(gateway_base_url)
    thread_id = _create_thread(gateway_base_url, OWNER_ID)
    observation = _run_first_turn(
        base_url=gateway_base_url,
        user_id=OWNER_ID,
        thread_id=thread_id,
        question=QUESTION,
        request_identity=f"phase03-start-{acceptance_id}",
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    review = _review_start_gateway(observation, thread_id=thread_id)
    store = PostgresConversationStore.from_env()
    try:
        store.apply_schema()
        persistence = _review_start_persistence(
            store,
            observation,
            thread_id=thread_id,
        )
    finally:
        store.connection.close()
    review["persistence"] = persistence
    review["passed"] = bool(review["passed"] and persistence["passed"])
    record = {
        "schema_version": "single-authority-phase03-acceptance-start.v1",
        "acceptance_id": acceptance_id,
        "question": QUESTION,
        "owner_id": OWNER_ID,
        "thread_id": thread_id,
        "run_id": observation.get("run_id"),
        "gateway_base_url": gateway_base_url,
        "gateway_observation": observation,
        "review": review,
        "artifact_directory": str(output_dir.resolve()),
    }
    _write_new_json(output_dir / "case-b-start.json", record)
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 0 if review["passed"] else 3


def run_case_b_execute(
    *,
    output_dir: Path,
    option_id: str,
    gateway_base_url: str | None,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> int:
    output_dir = _phase03_artifact_path(output_dir.resolve())
    result_path = output_dir / "case-b-execution.json"
    if result_path.exists():
        raise FileExistsError(result_path)
    start = _load_start_record(output_dir)
    option = _selected_option(start, option_id)
    persisted_base_url = _gateway_base_url(str(start.get("gateway_base_url") or ""))
    if gateway_base_url is not None:
        requested_base_url = _gateway_base_url(gateway_base_url)
        if requested_base_url != persisted_base_url:
            raise ValueError("phase03_acceptance_gateway_base_url_mismatch")
    run_id = str(start.get("run_id") or "")
    answer = str(option["label"])
    observation = _submit_clarification_resolution(
        base_url=persisted_base_url,
        user_id=OWNER_ID,
        run_id=run_id,
        answer=answer,
        selected_option_id=option_id,
        request_identity=f"phase03-execute-{start['acceptance_id']}",
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    if (
        observation.get("checkpoint_reached") is not True
        or observation.get("timed_out") is True
    ):
        raise RuntimeError("phase03_acceptance_gateway_observation_incomplete")
    if observation.get("terminal_status") in POST_EXECUTION_NON_PUBLICATION_TERMINALS:
        return _write_nonpublication_terminal_execution(
            output_dir=output_dir,
            result_path=result_path,
            start=start,
            option_id=option_id,
            gateway_base_url=persisted_base_url,
            observation=observation,
        )
    if observation.get("terminal_status") == "failed":
        return _write_failed_run_execution(
            output_dir=output_dir,
            result_path=result_path,
            start=start,
            option_id=option_id,
            gateway_base_url=persisted_base_url,
            observation=observation,
        )
    public_projection = _execution_projection_from_events(observation)

    store = PostgresConversationStore.from_env()
    try:
        store.apply_schema()
        execution = _load_persisted_execution(store, run_id)
        authority_review = _review_execution_result(execution)
        persistence_review = _review_execution_persistence(
            store,
            execution,
        )
    finally:
        store.connection.close()
    projection_review = _review_customer_projection(
        public_projection,
        execution,
    )
    terminal_review = _review_terminal_publication(
        observation=observation,
        gateway_base_url=persisted_base_url,
        run_id=run_id,
        thread_id=str(start["thread_id"]),
        selected_option_id=option_id,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    gateway_review = {
        "passed": bool(
            observation.get("checkpoint_reached") is True
            and observation.get("terminal_status") == "completed"
            and observation.get("publication_state") == "published"
            and observation.get("delivery_state") == "published"
            and observation.get("business_acceptance") == "passed"
            and observation.get("run_id") == run_id
        ),
        "terminal_status": observation.get("terminal_status"),
        "execution_result_ready": True,
    }
    passed = bool(
        gateway_review["passed"]
        and authority_review["passed"]
        and persistence_review["passed"]
        and projection_review["passed"]
        and terminal_review["terminal_state"]["acceptance_status"] == "passed"
    )
    review = {
        "schema_version": "single-authority-phase03-acceptance-review.v2",
        "passed": passed,
        "gateway": gateway_review,
        "execution_authority": authority_review,
        "persistence": persistence_review,
        "customer_projection": projection_review,
        "terminal_publication": terminal_review,
    }
    record = {
        "schema_version": "single-authority-phase03-acceptance-execution.v2",
        "acceptance_id": start["acceptance_id"],
        "question": start["question"],
        "owner_id": OWNER_ID,
        "thread_id": start["thread_id"],
        "run_id": run_id,
        "selected_option_id": option_id,
        "gateway_base_url": persisted_base_url,
        "gateway_observation": observation,
        "public_execution_result": public_projection,
        "review": review,
        "artifact_directory": str(output_dir),
    }
    _write_new_json(result_path, record)
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 0 if passed else 4


def _write_nonpublication_terminal_execution(
    *,
    output_dir: Path,
    result_path: Path,
    start: Mapping[str, Any],
    option_id: str,
    gateway_base_url: str,
    observation: Mapping[str, Any],
) -> int:
    terminal_status = str(observation.get("terminal_status") or "")
    post_execution_status = str(observation.get("post_execution_status") or "")
    post_execution = _require_post_execution_state(observation.get("post_execution"))
    if (
        terminal_status not in POST_EXECUTION_NON_PUBLICATION_TERMINALS
        or post_execution_status != terminal_status
        or post_execution["post_execution_status"] != terminal_status
        or observation.get("checkpoint_reached") is not True
        or observation.get("run_status") != "completed"
        or observation.get("timed_out") is not False
        or observation.get("customer_publication") is not None
        or observation.get("publication") is not None
    ):
        raise ValueError("phase03_acceptance_nonpublication_terminal_invalid")
    actual_states = (
        observation.get("publication_state"),
        observation.get("delivery_state"),
        observation.get("business_acceptance"),
    )
    if actual_states != POST_EXECUTION_NON_PUBLICATION_OUTCOMES[terminal_status]:
        raise ValueError("phase03_acceptance_nonpublication_terminal_invalid")

    terminal_failure = {
        "run_status": "completed",
        "post_execution_status": post_execution_status,
        "publication_state": actual_states[0],
        "delivery_state": actual_states[1],
        "business_acceptance": actual_states[2],
    }
    review = {
        "schema_version": "single-authority-phase03-terminal-review.v1",
        "passed": False,
        "gateway": {
            "passed": False,
            "checkpoint_reached": True,
            "run_status": "completed",
            "terminal_status": terminal_status,
            "post_execution_status": post_execution_status,
        },
        "terminal_failure": terminal_failure,
        "terminal_publication_review_performed": False,
    }
    record = {
        "schema_version": ("single-authority-phase03-acceptance-terminal-failure.v1"),
        "acceptance_id": start["acceptance_id"],
        "question": start["question"],
        "owner_id": OWNER_ID,
        "thread_id": start["thread_id"],
        "run_id": start["run_id"],
        "selected_option_id": option_id,
        "gateway_base_url": gateway_base_url,
        "gateway_observation": dict(observation),
        "public_execution_result": None,
        "review": review,
        "artifact_directory": str(output_dir),
    }
    _write_new_json(result_path, record)
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 4


def _write_failed_run_execution(
    *,
    output_dir: Path,
    result_path: Path,
    start: Mapping[str, Any],
    option_id: str,
    gateway_base_url: str,
    observation: Mapping[str, Any],
) -> int:
    if (
        observation.get("checkpoint_reached") is not True
        or observation.get("run_status") != "failed"
        or observation.get("terminal_status") != "failed"
        or observation.get("publication_state") != "not_ready"
        or observation.get("delivery_state") != "pending"
        or observation.get("business_acceptance") != "failed"
        or observation.get("timed_out") is not False
        or observation.get("customer_publication") is not None
        or observation.get("publication") is not None
        or observation.get("post_execution") is not None
    ):
        raise ValueError("phase03_acceptance_failed_terminal_invalid")

    events = observation.get("events")
    execution_events = [
        event
        for event in events or ()
        if isinstance(event, Mapping) and event.get("event") == "execution_result_ready"
    ]
    if len(execution_events) > 1:
        raise ValueError(
            "phase03_acceptance_execution_result_event_missing_or_ambiguous"
        )
    public_projection = (
        _execution_projection_from_events(observation) if execution_events else None
    )
    last_authoritative_stage = (
        "evidence_ready" if public_projection is not None else "pre_evidence"
    )
    review = {
        "schema_version": "single-authority-phase03-terminal-review.v1",
        "passed": False,
        "gateway": {
            "passed": False,
            "checkpoint_reached": True,
            "run_status": "failed",
            "terminal_status": "failed",
        },
        "terminal_failure": {
            "run_status": "failed",
            "last_authoritative_stage": last_authoritative_stage,
            "publication_state": "not_ready",
            "delivery_state": "pending",
            "business_acceptance": "failed",
        },
        "terminal_publication_review_performed": False,
    }
    record = {
        "schema_version": "single-authority-phase03-acceptance-run-failure.v1",
        "acceptance_id": start["acceptance_id"],
        "question": start["question"],
        "owner_id": OWNER_ID,
        "thread_id": start["thread_id"],
        "run_id": start["run_id"],
        "selected_option_id": option_id,
        "gateway_base_url": gateway_base_url,
        "gateway_observation": dict(observation),
        "public_execution_result": public_projection,
        "review": review,
        "artifact_directory": str(output_dir),
    }
    _write_new_json(result_path, record)
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 4


def reassess_case_b_execution(output_dir: Path) -> int:
    output_dir = _phase03_artifact_path(output_dir.resolve())
    record = _load_json(output_dir / "case-b-execution.json")
    if (
        record.get("schema_version")
        != "single-authority-phase03-acceptance-execution.v2"
        or record.get("artifact_directory") != str(output_dir)
        or record.get("question") != QUESTION
        or record.get("owner_id") != OWNER_ID
    ):
        raise ValueError("phase03_acceptance_execution_record_invalid")
    run_id = str(record.get("run_id") or "")
    projection = record.get("public_execution_result")
    if not isinstance(projection, Mapping):
        raise ValueError("phase03_acceptance_projection_missing")
    store = PostgresConversationStore.from_env()
    try:
        execution = _load_persisted_execution(store, run_id)
        authority_review = _review_execution_result(execution)
        persistence_review = _review_execution_persistence(
            store,
            execution,
        )
    finally:
        store.connection.close()
    projection_review = _review_customer_projection(projection, execution)
    observation = record.get("gateway_observation")
    if not isinstance(observation, Mapping):
        raise ValueError("phase03_acceptance_gateway_observation_missing")
    terminal_review = _review_terminal_publication(
        observation=observation,
        gateway_base_url=_gateway_base_url(str(record.get("gateway_base_url") or "")),
        run_id=run_id,
        thread_id=str(record.get("thread_id") or ""),
        selected_option_id=str(record.get("selected_option_id") or ""),
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
    )
    passed = bool(
        authority_review["passed"]
        and persistence_review["passed"]
        and projection_review["passed"]
        and terminal_review["terminal_state"]["acceptance_status"] == "passed"
    )
    report = {
        "schema_version": "single-authority-phase03-reassessment.v2",
        "acceptance_id": record.get("acceptance_id"),
        "run_id": run_id,
        "passed": passed,
        "execution_authority": authority_review,
        "persistence": persistence_review,
        "customer_projection": projection_review,
        "terminal_publication": terminal_review,
        "artifact_directory": str(output_dir),
    }
    _write_new_json(output_dir / "case-b-reassessment.json", report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if passed else 5


def _review_start_gateway(
    observation: Mapping[str, Any],
    *,
    thread_id: str,
) -> dict[str, Any]:
    clarification = _clarification_from_events(observation)
    options = clarification.get("options")
    option_ids = [
        str(item.get("option_id") or "")
        for item in options or ()
        if isinstance(item, Mapping)
    ]
    recommended = [
        str(item.get("option_id") or "")
        for item in options or ()
        if isinstance(item, Mapping) and item.get("recommended") is True
    ]
    question = clarification.get("question")
    option_records_valid = bool(
        isinstance(options, list)
        and all(
            isinstance(item, Mapping)
            and set(item)
            >= {
                "option_id",
                "label",
                "description",
                "recommended",
            }
            and isinstance(item.get("option_id"), str)
            and bool(item["option_id"].strip())
            and isinstance(item.get("label"), str)
            and bool(item["label"].strip())
            and isinstance(item.get("description"), str)
            and bool(item["description"].strip())
            and isinstance(item.get("recommended"), bool)
            for item in options
        )
    )
    passed = bool(
        observation.get("checkpoint_reached") is True
        and observation.get("terminal_status") == "waiting_for_clarification"
        and observation.get("thread_id") == thread_id
        and str(observation.get("run_id") or "")
        and isinstance(question, str)
        and bool(question.strip())
        and question.strip() != "待确认的业务澄清问题"
        and isinstance(options, list)
        and option_records_valid
        and 3 <= len(options) <= 4
        and len(option_ids) == len(options)
        and len(option_ids) == len(set(option_ids))
        and option_ids[-1:] == ["tell_agent_differently"]
        and recommended == ["comparison_baseline.previous_day"]
    )
    return {
        "passed": passed,
        "terminal_status": observation.get("terminal_status"),
        "option_ids": option_ids,
        "recommended_option_ids": recommended,
        "clarification": clarification,
    }


def _review_start_persistence(
    store: Any,
    observation: Mapping[str, Any],
    *,
    thread_id: str,
) -> dict[str, Any]:
    run_id = str(observation.get("run_id") or "")
    run_state = store.get_run_state(run_id)
    open_clarification = store.get_open_clarification(thread_id)
    active_intent = store.resolve_active_intent_revision(run_id)
    ledger = (
        store.load_decision_ledger(active_intent.intent_revision_id)
        if active_intent is not None
        else None
    )
    latest_transition_id = store.latest_accepted_transition_id(run_id)
    transition_row = store._fetchone(
        """
        SELECT node_name, acceptance_state, next_transition
        FROM waje_runtime.workflow_transition_attempts
        WHERE run_attempt_id = %(run_id)s
          AND transition_id = %(transition_id)s
        """,
        {"run_id": run_id, "transition_id": latest_transition_id},
    )
    transition_values = _row_values(
        transition_row,
        ("node_name", "acceptance_state", "next_transition"),
    )
    passed = bool(
        isinstance(run_state, Mapping)
        and run_state.get("thread_id") == thread_id
        and run_state.get("status") == "waiting_for_clarification"
        and open_clarification is not None
        and open_clarification.status == "waiting"
        and open_clarification.run_id == run_id
        and bool(open_clarification.question.strip())
        and open_clarification.question.strip() != "待确认的业务澄清问题"
        and len(open_clarification.options) >= 3
        and active_intent is not None
        and ledger is not None
        and ledger.position == 0
        and latest_transition_id
        and transition_values
        == (
            "persist_waiting_for_decision",
            "accepted",
            "await_user_decision",
        )
    )
    return {
        "passed": passed,
        "intent_revision_id": (
            active_intent.intent_revision_id if active_intent else None
        ),
        "decision_ledger_position": ledger.position if ledger else None,
        "accepted_transition_id": latest_transition_id,
    }


def _review_terminal_publication(
    *,
    observation: Mapping[str, Any],
    gateway_base_url: str,
    run_id: str,
    thread_id: str,
    selected_option_id: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    event_publication = {
        "customer_publication": (
            terminal_acceptance.gateway_once._require_customer_publication(
                observation.get("customer_publication")
            )
        ),
        "publication": terminal_acceptance.gateway_once._require_safe_publication(
            observation.get("publication")
        ),
    }
    dependency_health = terminal_acceptance._dependency_health(
        gateway_base_url,
        OWNER_ID,
    )
    submitted_decision = terminal_acceptance._submitted_decision(
        source_run_id=run_id,
        selected_option_id=selected_option_id,
        free_text=None,
    )
    with terminal_acceptance._connect_runtime_database() as connection:
        snapshot = terminal_acceptance._wait_for_terminal_snapshot(
            connection,
            run_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        if snapshot.get("thread_id") != thread_id:
            raise ValueError("phase03_acceptance_terminal_thread_mismatch")
        authority_records = terminal_acceptance._authority_records(
            connection,
            run_id,
        )
        persisted_publication = terminal_acceptance._persisted_publication(
            connection,
            run_id,
        )
        llm_call_audits = terminal_acceptance._llm_call_audits(
            connection,
            (run_id,),
        )
        human_decisions = terminal_acceptance._human_decisions(
            connection,
            (run_id,),
            submitted_decision,
        )
    return terminal_acceptance.build_acceptance_summary(
        case={
            "id": "case-b-single-authority-full-chain",
            "question_family": "factor_diagnosis",
            "variant": "additional",
            "turns": [
                {
                    "user": QUESTION,
                    "review_focus": (
                        "Full-chain authority, lifecycle, evidence, publication, "
                        "and delivery closure."
                    ),
                }
            ],
        },
        dependency_health=dependency_health,
        snapshot=snapshot,
        run_ids=(run_id,),
        authority_records=authority_records,
        persisted_publication=persisted_publication,
        event_publication=event_publication,
        llm_call_audits=llm_call_audits,
        human_decisions=human_decisions,
    )


def _load_persisted_execution(
    store: Any,
    run_id: str,
) -> AuthoritativeExecutionResult:
    run_state = store.get_run_state(run_id)
    if not isinstance(run_state, Mapping) or run_state.get("status") != "completed":
        raise ValueError("phase03_acceptance_run_not_completed")
    plan = store.resolve_active_plan_revision(run_id)
    if plan is None:
        raise ValueError("phase03_acceptance_active_plan_missing")
    snapshot = store.load_execution_snapshot(plan.plan_revision_id)
    if snapshot is None:
        raise ValueError("phase03_acceptance_execution_snapshot_missing")
    transition_input = capability_execution_transition_input(
        plan,
        hard_budget_limit=None,
    )
    accepted = store.load_accepted_transition(
        run_attempt_id=run_id,
        node_name="execute_capability_dag",
        input_digest=canonical_digest(transition_input),
    )
    if not isinstance(accepted, Mapping):
        raise ValueError("phase03_acceptance_settlement_transition_missing")
    transition = accepted.get("transition")
    transition_output = accepted.get("output_payload")
    try:
        output_snapshot = ExecutionSnapshot.from_dict(
            transition_output["execution_snapshot"]
        )
        stop_record = ExplorationStopRecord.from_dict(
            transition_output["exploration_stop_record"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("phase03_acceptance_settlement_output_invalid") from exc
    bundles = tuple(
        store.load_capability_outcome(plan.plan_revision_id, task.task_id)
        for task in plan.capability_tasks
    )
    if (
        not isinstance(transition, DurableTransition)
        or canonical_value(accepted.get("input_payload"))
        != canonical_value(transition_input)
        or output_snapshot != snapshot
        or any(bundle is None for bundle in bundles)
    ):
        raise ValueError("phase03_acceptance_execution_authority_incomplete")
    execution = AuthoritativeExecutionResult.from_records(
        plan_revision=plan,
        execution_snapshot=snapshot,
        exploration_stop_record=stop_record,
        capability_outcome_bundles=tuple(
            bundle for bundle in bundles if bundle is not None
        ),
        durable_transition=transition,
    )
    refs = run_state.get("request", {}).get("execution_result_refs")
    expected_refs = {
        "schema_version": execution.schema_version,
        "authoritative_execution_result_ref": (
            execution.authoritative_execution_result_ref
        ),
        "intent_revision_id": execution.intent_revision_id,
        "authority_context_ref": execution.authority_context_ref,
        "plan_revision_id": execution.plan_revision_id,
        "execution_snapshot_ref": execution.execution_snapshot_ref,
        "stop_ref": execution.stop_ref,
        "accepted_transition_id": execution.transition_id,
    }
    if canonical_value(refs) != canonical_value(expected_refs):
        raise ValueError("phase03_acceptance_execution_refs_mismatch")
    return execution


def _review_execution_result(
    execution: AuthoritativeExecutionResult,
) -> dict[str, Any]:
    plan = execution.plan_revision
    task_ids = {task.task_id for task in plan.capability_tasks}
    outcome_task_ids = {
        bundle[1].task_id for bundle in execution.capability_outcome_bundles
    }
    outcome_refs = tuple(
        sorted(bundle[1].outcome_ref for bundle in execution.capability_outcome_bundles)
    )
    evidence_refs = tuple(
        sorted(
            entry.entry_ref
            for bundle in execution.capability_outcome_bundles
            for entry in bundle[2]
        )
    )
    failure_refs = tuple(
        sorted(
            failure.failure_ref
            for bundle in execution.capability_outcome_bundles
            for failure in bundle[3]
        )
    )
    registered = set(builtin_capability_adapter_registry().capability_ids)
    missing_adapters = sorted(
        {
            task.capability_id
            for task in plan.capability_tasks
            if task.capability_id not in registered
        }
    )
    required_obligations = _review_required_obligation_coverage(execution)
    passed = bool(
        execution.status == "evidence_ready"
        and bool(task_ids)
        and len(task_ids) == len(plan.capability_tasks)
        and not missing_adapters
        and outcome_task_ids == task_ids
        and len(execution.capability_outcome_bundles) == len(task_ids)
        and outcome_refs == execution.execution_snapshot.outcome_refs
        and outcome_refs == execution.exploration_stop_record.evaluated_outcome_refs
        and evidence_refs == execution.execution_snapshot.evidence_entry_refs
        and failure_refs == execution.execution_snapshot.failure_refs
        and execution.exploration_stop_record.reason == "plan_exhausted"
        and required_obligations["passed"]
    )
    return {
        "passed": passed,
        "status": execution.status,
        "plan_revision_id": execution.plan_revision_id,
        "execution_snapshot_ref": execution.execution_snapshot_ref,
        "task_count": len(plan.capability_tasks),
        "outcome_count": len(execution.capability_outcome_bundles),
        "evidence_count": len(evidence_refs),
        "failure_count": len(failure_refs),
        "capability_ids": sorted(
            {task.capability_id for task in plan.capability_tasks}
        ),
        "missing_adapter_capability_ids": missing_adapters,
        "stop_reason": execution.exploration_stop_record.reason,
        "outcome_closure": outcome_refs == execution.execution_snapshot.outcome_refs,
        "evidence_closure": evidence_refs
        == execution.execution_snapshot.evidence_entry_refs,
        "failure_closure": failure_refs == execution.execution_snapshot.failure_refs,
        "required_obligations": required_obligations,
    }


def _review_required_obligation_coverage(
    execution: AuthoritativeExecutionResult,
) -> dict[str, Any]:
    required = tuple(
        obligation
        for obligation in execution.plan_revision.claim_obligations
        if obligation.role == "user_required"
    )
    states = []
    for obligation in required:
        evidence_refs = tuple(
            sorted(
                entry.entry_ref
                for _attempt, outcome, entries, _failures in (
                    execution.capability_outcome_bundles
                )
                if outcome.status == "succeeded"
                and obligation.obligation_id in outcome.affected_obligation_ids
                for entry in entries
                if entry.execution_state == "available"
                and obligation.claim_kind in entry.supported_claim_kinds
            )
        )
        states.append(
            {
                "obligation_id": obligation.obligation_id,
                "claim_kind": obligation.claim_kind,
                "status": "satisfied" if evidence_refs else "unresolved",
                "evidence_refs": evidence_refs,
            }
        )
    return {
        "passed": bool(required)
        and all(item["status"] == "satisfied" for item in states),
        "states": states,
    }


def _review_execution_persistence(
    store: Any,
    execution: AuthoritativeExecutionResult,
) -> dict[str, Any]:
    plan = execution.plan_revision
    expected_input, expected_output = capability_execution_transition_payloads(
        plan,
        execution.execution_snapshot,
        execution.exploration_stop_record,
    )
    accepted = store.load_accepted_transition(
        run_attempt_id=execution.run_attempt_id,
        node_name="execute_capability_dag",
        input_digest=canonical_digest(expected_input),
    )
    counts_row = store._fetchone(
        """
        SELECT
          (SELECT count(*)
           FROM waje_runtime.exploration_stop_records
           WHERE run_attempt_id = %(run_id)s
             AND stop_ref = %(stop_ref)s) AS stop_count,
          (SELECT count(*)
           FROM waje_runtime.capability_execution_snapshots
           WHERE run_attempt_id = %(run_id)s
             AND execution_snapshot_ref = %(snapshot_ref)s) AS snapshot_count,
          (SELECT count(*)
           FROM waje_runtime.workflow_transition_attempts
           WHERE run_attempt_id = %(run_id)s
             AND node_name = 'execute_capability_dag'
             AND acceptance_state = 'accepted') AS transition_count
        """,
        {
            "run_id": execution.run_attempt_id,
            "stop_ref": execution.stop_ref,
            "snapshot_ref": execution.execution_snapshot_ref,
        },
    )
    counts = _row_values(
        counts_row,
        ("stop_count", "snapshot_count", "transition_count"),
    )
    persisted_bundles = tuple(
        store.load_capability_outcome(plan.plan_revision_id, task.task_id)
        for task in plan.capability_tasks
    )
    claim_coverage = _review_claim_coverage_persistence(store, execution)
    passed = bool(
        store.resolve_active_plan_revision(execution.run_attempt_id) == plan
        and store.load_execution_snapshot(plan.plan_revision_id)
        == execution.execution_snapshot
        and tuple(
            sorted(
                (bundle for bundle in persisted_bundles if bundle is not None),
                key=lambda bundle: bundle[1].outcome_ref,
            )
        )
        == execution.capability_outcome_bundles
        and isinstance(accepted, Mapping)
        and accepted.get("transition") == execution.durable_transition
        and canonical_value(accepted.get("input_payload"))
        == canonical_value(expected_input)
        and canonical_value(accepted.get("output_payload"))
        == canonical_value(expected_output)
        and counts == (1, 1, 1)
        and claim_coverage["passed"]
        and bool(store.latest_accepted_transition_id(execution.run_attempt_id))
    )
    return {
        "passed": passed,
        "accepted_execute_transition_count": (counts[2] if len(counts) == 3 else None),
        "stop_record_count": counts[0] if len(counts) == 3 else None,
        "execution_snapshot_count": (counts[1] if len(counts) == 3 else None),
        "accepted_transition_id": execution.transition_id,
        "claim_coverage": claim_coverage,
        "latest_accepted_transition_id": (
            store.latest_accepted_transition_id(execution.run_attempt_id)
        ),
    }


def _review_claim_coverage_persistence(
    store: Any,
    execution: AuthoritativeExecutionResult,
) -> dict[str, Any]:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    authority_context = store.load_authority_context(execution.run_attempt_id)
    if authority_context is None:
        return {
            "passed": False,
            "decision": None,
            "checkpoint_ref": None,
            "accepted_transition_id": None,
        }
    evaluation = evaluate_claim_coverage(
        authority_context=authority_context,
        plan_revision=execution.plan_revision,
        execution_result=execution,
        route_catalog=registry,
    )
    transition_input = {
        "source_plan_revision_id": evaluation.source_plan_revision_id,
        "source_plan_digest": evaluation.source_plan_digest,
        "source_execution_result_ref": (evaluation.source_execution_result_ref),
        "source_execution_result_digest": (evaluation.source_execution_result_digest),
        "claim_coverage_evaluation_ref": evaluation.evaluation_ref,
        "claim_coverage_evaluation_digest": evaluation.content_digest,
    }
    accepted = store.load_accepted_transition(
        run_attempt_id=execution.run_attempt_id,
        node_name="evaluate_claim_coverage",
        input_digest=canonical_digest(transition_input),
    )
    try:
        output = accepted["output_payload"]
        transition = accepted["transition"]
        replayed_evaluation = ClaimCoverageEvaluation.from_dict(
            output["claim_coverage_evaluation"],
            authority_context=authority_context,
            plan_revision=execution.plan_revision,
            execution_result=execution,
            route_catalog=registry,
        )
        decision = PlanExpansionDecision.from_dict(
            output["plan_expansion_decision"],
            evaluation=replayed_evaluation,
        )
        raw_patch = output["plan_patch"]
        patch = (
            None
            if raw_patch is None
            else PlanPatch.from_dict(
                raw_patch,
                plan_revision=execution.plan_revision,
                execution_result=execution,
                evaluation=replayed_evaluation,
                decision=decision,
            )
        )
        checkpoint = ClaimCoverageCheckpoint.create(
            plan_revision=execution.plan_revision,
            execution_result=execution,
            evaluation=replayed_evaluation,
            decision=decision,
            plan_patch=patch,
            transition=transition,
        )
    except (KeyError, TypeError, ValueError):
        return {
            "passed": False,
            "decision": None,
            "checkpoint_ref": None,
            "accepted_transition_id": None,
        }
    expected_input, expected_output = claim_coverage_transition_payloads(
        evaluation=replayed_evaluation,
        decision=decision,
        plan_patch=patch,
    )
    run_state = store.get_run_state(execution.run_attempt_id)
    persisted_refs = (
        run_state.get("request", {}).get("claim_coverage_refs")
        if isinstance(run_state, Mapping)
        else None
    )
    expected_refs = {
        "schema_version": checkpoint.schema_version,
        "source_plan_revision_id": checkpoint.source_plan_revision_id,
        "source_execution_result_ref": (checkpoint.source_execution_result_ref),
        "claim_coverage_checkpoint_ref": checkpoint.checkpoint_ref,
        "claim_coverage_checkpoint_digest": checkpoint.content_digest,
        "claim_coverage_evaluation_ref": checkpoint.evaluation_ref,
        "plan_expansion_decision_ref": checkpoint.decision_ref,
        "decision": checkpoint.decision.decision,
        "plan_patch_ref": checkpoint.plan_patch_ref,
        "accepted_transition_id": checkpoint.transition_id,
    }
    passed = bool(
        decision.decision == "seal"
        and patch is None
        and transition.parent_transition_id == execution.transition_id
        and canonical_value(accepted.get("input_payload"))
        == canonical_value(expected_input)
        and canonical_value(output) == canonical_value(expected_output)
        and canonical_value(persisted_refs) == canonical_value(expected_refs)
    )
    return {
        "passed": passed,
        "decision": decision.decision,
        "checkpoint_ref": checkpoint.checkpoint_ref,
        "accepted_transition_id": checkpoint.transition_id,
    }


def _review_customer_projection(
    projection: Mapping[str, Any],
    execution: AuthoritativeExecutionResult,
) -> dict[str, Any]:
    expected = execution.public_projection()
    private_paths = _private_projection_paths(projection)
    serialized = json.dumps(
        canonical_value(projection),
        ensure_ascii=False,
        sort_keys=True,
    ).lower()
    serialized_markers = sorted(
        marker for marker in PRIVATE_PROJECTION_MARKERS if marker in serialized
    )
    passed = bool(
        set(projection) == PUBLIC_EXECUTION_FIELDS
        and canonical_value(projection) == canonical_value(expected)
        and not private_paths
        and not serialized_markers
    )
    return {
        "passed": passed,
        "field_names": sorted(projection),
        "matches_persisted_authority": canonical_value(projection)
        == canonical_value(expected),
        "private_paths": private_paths,
        "private_markers": serialized_markers,
    }


def _private_projection_paths(
    value: Any,
    path: str = "$",
) -> list[str]:
    leaks: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}"
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in PRIVATE_KEY_FRAGMENTS):
                leaks.append(child)
            leaks.extend(_private_projection_paths(item, child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            leaks.extend(_private_projection_paths(item, f"{path}[{index}]"))
    return leaks


def _execution_projection_from_events(
    observation: Mapping[str, Any],
) -> Mapping[str, Any]:
    events = observation.get("events")
    matches = [
        event
        for event in events or ()
        if isinstance(event, Mapping) and event.get("event") == "execution_result_ready"
    ]
    if len(matches) != 1:
        raise ValueError(
            "phase03_acceptance_execution_result_event_missing_or_ambiguous"
        )
    payload = matches[0].get("payload")
    projection = (
        payload.get("execution_result") if isinstance(payload, Mapping) else None
    )
    if (
        not isinstance(payload, Mapping)
        or payload.get("status") != "evidence_ready"
        or not isinstance(projection, Mapping)
    ):
        raise ValueError("phase03_acceptance_execution_result_event_invalid")
    return projection


def _clarification_from_events(
    observation: Mapping[str, Any],
) -> Mapping[str, Any]:
    events = observation.get("events")
    matches = [
        event.get("payload")
        for event in events or ()
        if isinstance(event, Mapping)
        and event.get("event") == "clarification_state_saved"
        and isinstance(event.get("payload"), Mapping)
    ]
    if len(matches) != 1:
        raise ValueError("phase03_acceptance_clarification_event_missing_or_ambiguous")
    return matches[0]


def _selected_option(
    start: Mapping[str, Any],
    option_id: str,
) -> Mapping[str, Any]:
    if (
        not option_id
        or option_id != option_id.strip()
        or option_id == "tell_agent_differently"
    ):
        raise ValueError("phase03_acceptance_option_id_invalid")
    review = start.get("review")
    clarification = review.get("clarification") if isinstance(review, Mapping) else None
    options = (
        clarification.get("options") if isinstance(clarification, Mapping) else None
    )
    matches = [
        option
        for option in options or ()
        if isinstance(option, Mapping) and option.get("option_id") == option_id
    ]
    if len(matches) != 1 or not str(matches[0].get("label") or "").strip():
        raise ValueError("phase03_acceptance_option_id_unknown")
    return matches[0]


def _load_start_record(output_dir: Path) -> dict[str, Any]:
    record = _load_json(output_dir / "case-b-start.json")
    if (
        record.get("schema_version") != "single-authority-phase03-acceptance-start.v1"
        or record.get("artifact_directory") != str(output_dir)
        or record.get("question") != QUESTION
        or record.get("owner_id") != OWNER_ID
        or not record.get("run_id")
        or not record.get("thread_id")
    ):
        raise ValueError("phase03_acceptance_start_record_invalid")
    return record


def _row_values(
    row: Any,
    field_names: Sequence[str],
) -> tuple[Any, ...]:
    if row is None:
        return ()
    if isinstance(row, Mapping):
        return tuple(row.get(field) for field in field_names)
    return tuple(row)


def _validate_timing(args: Any) -> None:
    for field in ("timeout_seconds", "poll_interval_seconds"):
        value = getattr(args, field, 1.0)
        if value <= 0:
            raise ValueError(f"phase03_acceptance_{field}_invalid")


def _gateway_base_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        raise ValueError("phase03_acceptance_gateway_base_url_invalid")
    return normalized


def _phase03_artifact_path(path: Path) -> Path:
    resolved = path.resolve()
    for legacy in (PHASE01_ROOT.resolve(), PHASE02_ROOT.resolve()):
        if (
            resolved == legacy
            or legacy in resolved.parents
            or resolved in legacy.parents
        ):
            raise ValueError("phase03_acceptance_artifact_root_reserved")
    return resolved


def _acceptance_id(label: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{label}-{stamp}-{uuid4().hex[:10]}"


def _new_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=False)
    return path


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("phase03_acceptance_artifact_invalid")
    return payload


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
