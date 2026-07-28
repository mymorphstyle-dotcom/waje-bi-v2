from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.agent_task_resume_outbox import (
    PostgresAgentTaskResumeOutbox,
    process_agent_task_resume_outbox,
)
from bi_agent.runtime.general_agent_turn_recovery import (
    recover_general_agent_turns,
)
from bi_agent.runtime.thread_summary_maintenance import (
    PostgresThreadSummaryMaintenance,
    process_stale_thread_summaries,
)


DispatchRunner = Callable[[Mapping[str, Any]], Mapping[str, Any]]

_COMMAND_OPTION_KEYS = frozenset(
    {
        "topicSelection",
        "topicChoiceAnswer",
        "intentRevisionContext",
        "clarification",
    }
)
_INTENT_REVISION_FIELDS = frozenset(
    {
        "goal_bindings",
        "desired_decisions",
        "analysis_axes",
        "target_metric_refs",
        "baseline_refs",
        "resolved_window_refs",
        "time_spec",
        "scope",
        "filters",
        "direction_premise",
    }
)


def load_worker_env_file(path: str) -> tuple[str, ...]:
    """Load local worker settings without overriding deployment environment."""

    if not isinstance(path, str) or not path or path != path.strip():
        raise ValueError("runtime_recovery_env_file_invalid")
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ()
    loaded: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value
        loaded.append(key)
    return tuple(loaded)


def run_agent_core_dispatch(dispatch: Mapping[str, Any]) -> Mapping[str, Any]:
    dispatch_id = _required_string(dispatch, "dispatch_id")
    run_id = _required_string(dispatch, "run_id")
    thread_id = _required_string(dispatch, "thread_id")
    producer_kind = _required_string(dispatch, "producer_kind")
    scope_ref = _required_string(dispatch, "scope_ref")
    owner_id = _required_string(dispatch, "dispatch_owner_id")
    lease_epoch = dispatch.get("lease_epoch")
    payload = dispatch.get("request_payload")
    if (
        producer_kind not in {"thread_message", "clarification_resolution"}
        or not isinstance(lease_epoch, int)
        or isinstance(lease_epoch, bool)
        or lease_epoch <= 0
        or not isinstance(payload, Mapping)
    ):
        raise ValueError("run_dispatch_recovery_payload_invalid")
    command = _validated_agent_core_command(
        payload,
        producer_kind=producer_kind,
        run_id=run_id,
    )
    user_message = command["message"]
    expected_scope_ref = (
        run_id if producer_kind == "clarification_resolution" else thread_id
    )
    if scope_ref != expected_scope_ref:
        raise ValueError("run_dispatch_recovery_scope_mismatch")

    core = ConversationAgentCore.from_environment()
    try:
        run_kwargs: dict[str, Any] = {
            "thread_id": thread_id,
            "run_id": run_id,
            "user_message": user_message,
            "run_dispatch": {
                "dispatch_id": dispatch_id,
                "dispatch_owner_id": owner_id,
                "lease_epoch": lease_epoch,
            },
        }
        if "topicSelection" in command:
            selection = command["topicSelection"]
            run_kwargs["topic_selection"] = {
                "source_run_id": selection["sourceRunId"],
                "topic_id": selection["topicId"],
            }
        if "topicChoiceAnswer" in command:
            choice_answer = command["topicChoiceAnswer"]
            run_kwargs["topic_choice_answer"] = {
                "source_run_id": choice_answer["sourceRunId"],
                "answer": choice_answer["answer"],
            }
        if "intentRevisionContext" in command:
            run_kwargs["intent_revision_context"] = command["intentRevisionContext"]
        if "clarification" in command:
            run_kwargs["clarification"] = command["clarification"]
        result = core.run_message(
            **run_kwargs,
        )
        if not isinstance(result, Mapping):
            raise ValueError("run_dispatch_recovery_result_invalid")
        result_run_id = result.get("run_id")
        status = result.get("status")
        if result_run_id not in (None, run_id) or status not in {
            "planned",
            "evidence_ready",
            "authority_sealed",
            "narrative_ready",
            "waiting_for_clarification",
            "completed",
            "interaction_completed",
            "failed",
        }:
            raise ValueError("run_dispatch_recovery_result_invalid")
        return result
    finally:
        connection = getattr(getattr(core, "store", None), "connection", None)
        close = getattr(connection, "close", None)
        if callable(close):
            close()


def recover_pending_run_dispatches(
    *,
    store: Any,
    dispatch_runner: DispatchRunner = run_agent_core_dispatch,
    limit: int = 100,
    thread_id: str | None = None,
) -> dict[str, list[Any]]:
    scope_kwargs = {} if thread_id is None else {"thread_id": thread_id}
    swept = list(store.sweep_expired_run_dispatches(limit=limit, **scope_kwargs))
    leases = tuple(
        # A run can hold the worker for several minutes. Leasing a batch up
        # front lets later leases expire before execution starts and can
        # starve newer work behind an old backlog. Each cycle therefore owns
        # at most one run dispatch.
        store.lease_recoverable_run_dispatches(limit=1, **scope_kwargs)
    )
    dispatched: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for lease in leases:
        dispatch_id = _required_string(lease, "dispatch_id")
        run_id = _required_string(lease, "run_id")
        thread_id = _required_string(lease, "thread_id")
        owner_id = _required_string(lease, "dispatch_owner_id")
        lease_epoch = lease.get("lease_epoch")
        if (
            not isinstance(lease_epoch, int)
            or isinstance(lease_epoch, bool)
            or lease_epoch <= 0
        ):
            raise ValueError("run_dispatch_recovery_lease_invalid")
        try:
            result = dispatch_runner(lease)
            if not isinstance(result, Mapping):
                raise ValueError("run_dispatch_recovery_result_invalid")
            status = result.get("status")
            if not isinstance(status, str) or not status:
                raise ValueError("run_dispatch_recovery_result_invalid")
            dispatched.append({"run_id": run_id, "status": status})
        except Exception as exc:
            failure_reason = "run_dispatch_recovery_worker_failed"
            try:
                durable = store.fail_owned_run_dispatch(
                    dispatch_id=dispatch_id,
                    run_id=run_id,
                    thread_id=thread_id,
                    dispatch_owner_id=owner_id,
                    lease_epoch=lease_epoch,
                    failure_reason=failure_reason,
                )
            except Exception as finalization_error:
                raise RuntimeError(
                    "run_dispatch_recovery_failure_finalization_failed"
                ) from finalization_error
            durable_status = (
                durable.get("status") if isinstance(durable, Mapping) else None
            )
            if durable_status == "failed":
                durable_reason = durable.get("failure_reason")
                failed.append(
                    {
                        "run_id": run_id,
                        "failure_reason": (
                            durable_reason
                            if isinstance(durable_reason, str) and durable_reason
                            else failure_reason
                        ),
                        "error_type": type(exc).__name__,
                    }
                )
            elif durable_status in {
                "planned",
                "evidence_ready",
                "authority_sealed",
                "narrative_ready",
                "waiting_for_clarification",
                "completed",
                "interaction_completed",
            }:
                dispatched.append({"run_id": run_id, "status": durable_status})
            else:
                raise RuntimeError("run_dispatch_recovery_failure_finalization_invalid")
    return {
        "swept": swept,
        "leased": [_required_string(lease, "run_id") for lease in leases],
        "dispatched": dispatched,
        "failed": failed,
    }


def _recover_source(
    source_name: str,
    operation: Callable[[], Mapping[str, Any]],
) -> Mapping[str, Any]:
    try:
        return dict(operation())
    except Exception as exc:
        return {
            "status": "failed",
            "error_code": "runtime_recovery_source_failed",
            "error_type": type(exc).__name__,
            "source": source_name,
        }


def run_runtime_recovery_cycle(
    *,
    limit: int = 100,
    worker_id: str,
    thread_id: str | None = None,
) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("runtime_recovery_limit_invalid")
    if not worker_id or worker_id != worker_id.strip():
        raise ValueError("runtime_recovery_worker_id_invalid")
    if thread_id is not None and (
        not isinstance(thread_id, str)
        or not thread_id
        or thread_id != thread_id.strip()
    ):
        raise ValueError("runtime_recovery_thread_id_invalid")
    scope_kwargs = {} if thread_id is None else {"thread_id": thread_id}
    store = PostgresConversationStore.from_env()
    try:
        summary: dict[str, Any] = {
            "run_dispatches": _recover_source(
                "run_dispatches",
                lambda: recover_pending_run_dispatches(
                    store=store,
                    limit=limit,
                    **scope_kwargs,
                ),
            ),
            "general_agent_turns": _recover_source(
                "general_agent_turns",
                lambda: recover_general_agent_turns(
                    store=store,
                    limit=limit,
                    **scope_kwargs,
                ),
            ),
            "thread_summary_refreshes": _recover_source(
                "thread_summary_refreshes",
                lambda: process_stale_thread_summaries(
                    maintenance=PostgresThreadSummaryMaintenance(store.connection),
                    limit=limit,
                    **scope_kwargs,
                ),
            ),
            "agent_task_resumes": _recover_source(
                "agent_task_resumes",
                lambda: process_agent_task_resume_outbox(
                    outbox=PostgresAgentTaskResumeOutbox(store.connection),
                    limit=limit,
                    worker_id=worker_id,
                    **scope_kwargs,
                ),
            ),
        }
        return summary
    finally:
        store.connection.close()


RecoveryCycleRunner = Callable[..., Mapping[str, Any]]


def run_runtime_recovery_worker(
    *,
    limit: int = 100,
    poll_interval_seconds: float = 2.0,
    once: bool = False,
    worker_id: str | None = None,
    thread_id: str | None = None,
    stop_event: threading.Event | None = None,
    cycle_runner: RecoveryCycleRunner = run_runtime_recovery_cycle,
    output: Any = None,
) -> int:
    if (
        isinstance(poll_interval_seconds, bool)
        or not math.isfinite(poll_interval_seconds)
        or poll_interval_seconds <= 0
    ):
        raise ValueError("runtime_recovery_poll_interval_invalid")
    resolved_worker_id = worker_id or (
        f"runtime-recovery:{os.getpid()}:{uuid.uuid4().hex}"
    )
    if not resolved_worker_id or resolved_worker_id != resolved_worker_id.strip():
        raise ValueError("runtime_recovery_worker_id_invalid")
    if thread_id is not None and (
        not isinstance(thread_id, str)
        or not thread_id
        or thread_id != thread_id.strip()
    ):
        raise ValueError("runtime_recovery_thread_id_invalid")
    stop = stop_event or threading.Event()
    stream = output or sys.stdout
    while not stop.is_set():
        try:
            cycle_kwargs: dict[str, Any] = {
                "limit": limit,
                "worker_id": resolved_worker_id,
            }
            if thread_id is not None:
                cycle_kwargs["thread_id"] = thread_id
            summary = dict(cycle_runner(**cycle_kwargs))
            record: dict[str, Any] = {
                "schemaVersion": "runtime-recovery-cycle.v1",
                "status": "completed",
                "workerId": resolved_worker_id,
                "summary": summary,
            }
            exit_code = 0
        except Exception as exc:
            record = {
                "schemaVersion": "runtime-recovery-cycle.v1",
                "status": "failed",
                "workerId": resolved_worker_id,
                "errorCode": "runtime_recovery_cycle_failed",
                "errorType": type(exc).__name__,
            }
            exit_code = 1
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        if once:
            return exit_code
        stop.wait(poll_interval_seconds)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Continuously recover committed WAJE BI dispatches, General Agent "
            "turns, BI-to-Agent task resumes, and stale thread summaries."
        ),
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--thread-id")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=float(os.getenv("WAJE_RUNTIME_WORKER_POLL_SECONDS", "2")),
    )
    args = parser.parse_args()
    load_worker_env_file(args.env_file)
    stop = threading.Event()
    if not args.once:
        _install_shutdown_handlers(stop)
    return run_runtime_recovery_worker(
        limit=args.limit,
        poll_interval_seconds=args.poll_interval_seconds,
        once=args.once,
        thread_id=args.thread_id,
        stop_event=stop,
    )


def _install_shutdown_handlers(stop: threading.Event) -> None:
    def request_shutdown(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)


def _required_string(values: Mapping[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("run_dispatch_recovery_payload_invalid")
    return value.strip()


def _validated_agent_core_command(
    payload: Mapping[str, Any],
    *,
    producer_kind: str,
    run_id: str,
) -> dict[str, Any]:
    keys = set(payload)
    if (
        "message" not in keys
        or not keys <= {"message", "resumeRequest", *_COMMAND_OPTION_KEYS}
        or len(keys & _COMMAND_OPTION_KEYS) > 1
    ):
        raise ValueError("run_dispatch_recovery_payload_invalid")
    message = _required_exact_string(payload, "message")
    command: dict[str, Any] = {"message": message}
    if producer_kind == "clarification_resolution":
        if keys != {"message", "clarification", "resumeRequest"}:
            raise ValueError("run_dispatch_recovery_payload_invalid")
        clarification = _validated_clarification(
            payload["clarification"],
            run_id=run_id,
        )
        if not isinstance(payload["resumeRequest"], Mapping):
            raise ValueError("run_dispatch_recovery_payload_invalid")
        if clarification["answer"] != message:
            raise ValueError("run_dispatch_recovery_payload_invalid")
        command["clarification"] = clarification
        return command
    if producer_kind != "thread_message" or "clarification" in keys:
        raise ValueError("run_dispatch_recovery_payload_invalid")
    if "topicSelection" in payload:
        command["topicSelection"] = _validated_topic_selection(
            payload["topicSelection"]
        )
    if "topicChoiceAnswer" in payload:
        answer = _validated_topic_choice_answer(payload["topicChoiceAnswer"])
        if answer["answer"] != message:
            raise ValueError("run_dispatch_recovery_payload_invalid")
        command["topicChoiceAnswer"] = answer
    if "intentRevisionContext" in payload:
        command["intentRevisionContext"] = _validated_intent_revision_context(
            payload["intentRevisionContext"]
        )
    return command


def _validated_clarification(value: Any, *, run_id: str) -> dict[str, Any]:
    expected = {
        "sourceRunId",
        "resolutionId",
        "attemptRunId",
        "answer",
        "selectedOptionIds",
        "source",
        "retryAttempt",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("run_dispatch_recovery_payload_invalid")
    selected_option_ids = value.get("selectedOptionIds")
    if (
        not isinstance(selected_option_ids, list)
        or any(
            not isinstance(option_id, str)
            or not option_id
            or option_id != option_id.strip()
            for option_id in selected_option_ids
        )
        or len(selected_option_ids) != len(set(selected_option_ids))
    ):
        raise ValueError("run_dispatch_recovery_payload_invalid")
    clarification = {
        "sourceRunId": _required_exact_string(value, "sourceRunId"),
        "resolutionId": _required_exact_string(value, "resolutionId"),
        "attemptRunId": _required_exact_string(value, "attemptRunId"),
        "answer": _required_exact_string(value, "answer"),
        "selectedOptionIds": selected_option_ids,
        "source": value.get("source"),
        "retryAttempt": value.get("retryAttempt"),
    }
    if (
        clarification["sourceRunId"] != run_id
        or clarification["attemptRunId"] != run_id
        or clarification["source"] != "user"
        or clarification["retryAttempt"] is not False
    ):
        raise ValueError("run_dispatch_recovery_payload_invalid")
    return clarification


def _validated_topic_selection(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "sourceRunId",
        "topicId",
    }:
        raise ValueError("run_dispatch_recovery_payload_invalid")
    return {
        "sourceRunId": _required_exact_string(value, "sourceRunId"),
        "topicId": _required_exact_string(value, "topicId"),
    }


def _validated_topic_choice_answer(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "sourceRunId",
        "answer",
    }:
        raise ValueError("run_dispatch_recovery_payload_invalid")
    return {
        "sourceRunId": _required_exact_string(value, "sourceRunId"),
        "answer": _required_exact_string(value, "answer"),
    }


def _validated_intent_revision_context(value: Any) -> dict[str, Any]:
    expected = {
        "supersedes_intent_revision_id",
        "superseded_plan_fields",
        "intent_revision_reason_ref",
        "parent_transition_id",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("run_dispatch_recovery_payload_invalid")
    raw_fields = value.get("superseded_plan_fields")
    if (
        not isinstance(raw_fields, list)
        or not raw_fields
        or any(
            not isinstance(item, str) or item not in _INTENT_REVISION_FIELDS
            for item in raw_fields
        )
        or len(set(raw_fields)) != len(raw_fields)
    ):
        raise ValueError("run_dispatch_recovery_payload_invalid")
    return {
        "supersedes_intent_revision_id": _required_exact_string(
            value, "supersedes_intent_revision_id"
        ),
        "superseded_plan_fields": list(raw_fields),
        "intent_revision_reason_ref": _required_exact_string(
            value, "intent_revision_reason_ref"
        ),
        "parent_transition_id": _required_exact_string(value, "parent_transition_id"),
    }


def _required_exact_string(values: Mapping[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("run_dispatch_recovery_payload_invalid")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
