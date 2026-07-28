from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


TERMINAL_CALL_STATUSES = frozenset({"succeeded", "failed"})


def _seconds(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds(), 3)


def _provider_attempts(connection: Any, run_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT attempt.attempt_ref, attempt.call_kind, attempt.operation_name,
               attempt.attempt_number, event.status, event.failure_code,
               event.created_at,
               COALESCE(event.output_payload, event.failure_payload) AS payload
        FROM waje_runtime.durable_call_attempts attempt
        JOIN waje_runtime.durable_call_attempt_events event
          ON event.attempt_ref = attempt.attempt_ref
        WHERE attempt.run_attempt_id = %s
          AND attempt.call_kind LIKE '%%_provider'
          AND event.status IN ('started', 'succeeded', 'failed')
        ORDER BY event.created_at, attempt.attempt_ref
        """,
        (run_id,),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    metadata: dict[str, dict[str, Any]] = {}
    for row in rows:
        grouped[str(row["attempt_ref"])].append(dict(row))
        metadata[str(row["attempt_ref"])] = dict(row)
    attempts: list[dict[str, Any]] = []
    for attempt_ref, events in grouped.items():
        started = next(
            (event["created_at"] for event in events if event["status"] == "started"),
            None,
        )
        terminal = next(
            (
                event
                for event in events
                if event["status"] in TERMINAL_CALL_STATUSES
            ),
            None,
        )
        row = metadata[attempt_ref]
        audit = (
            terminal["payload"].get("audit", {})
            if terminal is not None and isinstance(terminal["payload"], dict)
            else {}
        )
        attempts.append(
            {
                "attemptRef": attempt_ref,
                "callKind": str(row["call_kind"]),
                "operationName": str(row["operation_name"]),
                "attemptNumber": int(row["attempt_number"]),
                "status": str(terminal["status"]) if terminal else "started",
                "failureCode": (
                    str(terminal["failure_code"])
                    if terminal and terminal["failure_code"]
                    else None
                ),
                "startedAt": started,
                "finishedAt": terminal["created_at"] if terminal else None,
                "durationSeconds": (
                    _seconds(started, terminal["created_at"])
                    if started is not None and terminal is not None
                    else None
                ),
                "providerDurationMs": audit.get("duration_ms"),
                "inputBytes": audit.get("input_bytes"),
                "outputBytes": audit.get("output_bytes"),
                "provider": audit.get("provider"),
                "model": audit.get("model"),
            }
        )
    return sorted(
        attempts,
        key=lambda item: (
            item["startedAt"] or datetime.max,
            item["attemptRef"],
        ),
    )


def _operation_interval(
    attempts: list[dict[str, Any]],
    call_kind: str,
) -> tuple[datetime, datetime] | None:
    matches = [
        attempt
        for attempt in attempts
        if attempt["callKind"] == call_kind
        and attempt["startedAt"] is not None
        and attempt["finishedAt"] is not None
    ]
    if not matches:
        return None
    return (
        min(attempt["startedAt"] for attempt in matches),
        max(attempt["finishedAt"] for attempt in matches),
    )


def _segment(name: str, start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "name": name,
        "startedAt": start,
        "finishedAt": end,
        "durationSeconds": _seconds(start, end),
    }


def _run_profile(connection: Any, run_id: str) -> dict[str, Any]:
    run = connection.execute(
        """
        SELECT status, created_at, updated_at
        FROM waje_runtime.analysis_runs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"run_missing:{run_id}")
    dispatches = connection.execute(
        """
        SELECT request_identity, dispatch_state, terminal_status,
               lease_epoch, created_at, updated_at
        FROM waje_runtime.run_dispatches
        WHERE run_id = %s
        ORDER BY created_at, dispatch_id
        """,
        (run_id,),
    ).fetchall()
    if len(dispatches) < 2:
        raise ValueError(f"clarification_dispatch_pair_missing:{run_id}")
    attempts = _provider_attempts(connection, run_id)
    terminal_attempts = [
        attempt for attempt in attempts if attempt["finishedAt"] is not None
    ]
    if not terminal_attempts:
        raise ValueError(f"provider_attempts_missing:{run_id}")
    planner = _operation_interval(attempts, "planner_provider")
    semantic = _operation_interval(attempts, "semantic_provider")
    controlled = _operation_interval(
        attempts,
        "controlled_investigation_provider",
    )
    narrative = _operation_interval(attempts, "narrative_provider")
    if planner is None or semantic is None or narrative is None:
        raise ValueError(f"terminal_stage_interval_missing:{run_id}")
    run_created = run["created_at"]
    run_updated = run["updated_at"]
    first_provider_started = min(
        attempt["startedAt"]
        for attempt in attempts
        if attempt["startedAt"] is not None
    )
    first_dispatch_finished = dispatches[0]["updated_at"]
    continuation_created = dispatches[1]["created_at"]
    post_semantic_start = controlled[0] if controlled else narrative[0]
    segments = [
        _segment(
            "dispatch_queue_before_first_provider",
            run_created,
            first_provider_started,
        ),
        _segment(
            "initial_intent_and_clarification",
            first_provider_started,
            first_dispatch_finished,
        ),
        _segment(
            "clarification_decision_and_resume_gap",
            first_dispatch_finished,
            continuation_created,
        ),
        _segment(
            "continuation_dispatch_startup",
            continuation_created,
            planner[0],
        ),
        _segment("accepted_plan_materialization", planner[0], planner[1]),
        _segment("query_capability_and_evidence", planner[1], semantic[0]),
        _segment("claim_and_recommendation_authority", semantic[0], semantic[1]),
        _segment(
            "post_authority_local_processing",
            semantic[1],
            post_semantic_start,
        ),
    ]
    if controlled:
        segments.append(
            _segment(
                "controlled_investigation_critical_path",
                controlled[0],
                controlled[1],
            )
        )
    segments.extend(
        [
            _segment("narrative_writer", narrative[0], narrative[1]),
            _segment("verification_publication_delivery_tail", narrative[1], run_updated),
        ]
    )
    counts = {
        str(row["call_kind"]): int(row["count"])
        for row in connection.execute(
            """
            SELECT call_kind, count(*)
            FROM waje_runtime.durable_call_attempts
            WHERE run_attempt_id = %s
            GROUP BY call_kind
            ORDER BY call_kind
            """,
            (run_id,),
        ).fetchall()
    }
    provider_serial_seconds = round(
        sum(
            attempt["durationSeconds"] or 0.0
            for attempt in terminal_attempts
        ),
        3,
    )
    return {
        "runId": run_id,
        "status": str(run["status"]),
        "createdAt": run_created,
        "updatedAt": run_updated,
        "wallSeconds": _seconds(run_created, run_updated),
        "segments": segments,
        "callCounts": counts,
        "providerSerialEquivalentSeconds": provider_serial_seconds,
        "providerFailures": [
            {
                "callKind": attempt["callKind"],
                "operationName": attempt["operationName"],
                "attemptNumber": attempt["attemptNumber"],
                "failureCode": attempt["failureCode"],
                "durationSeconds": attempt["durationSeconds"],
            }
            for attempt in terminal_attempts
            if attempt["status"] == "failed"
        ],
        "providerAttempts": [
            {
                key: (
                    value.isoformat()
                    if isinstance(value, datetime)
                    else value
                )
                for key, value in attempt.items()
                if key != "attemptRef"
            }
            for attempt in attempts
        ],
    }


def _planner_hang_profile(
    connection: Any,
    hung_run_id: str,
    comparison_run_id: str,
    *,
    diagnostic_replay_seconds: float | None,
    diagnostic_input_bytes: int | None,
) -> dict[str, Any]:
    hung_run = connection.execute(
        """
        SELECT status, created_at, updated_at
        FROM waje_runtime.analysis_runs
        WHERE run_id = %s
        """,
        (hung_run_id,),
    ).fetchone()
    if hung_run is None:
        raise ValueError(f"run_missing:{hung_run_id}")
    hung_attempts = [
        attempt
        for attempt in _provider_attempts(connection, hung_run_id)
        if attempt["callKind"] == "planner_provider"
    ]
    comparison_attempts = [
        attempt
        for attempt in _provider_attempts(connection, comparison_run_id)
        if attempt["callKind"] == "planner_provider"
        and attempt["status"] == "succeeded"
    ]
    if len(hung_attempts) != 1 or not comparison_attempts:
        raise ValueError("planner_hang_comparison_missing")
    hung = hung_attempts[0]
    comparison = comparison_attempts[-1]
    configured_timeout = os.environ.get("WAJE_LLM_TIMEOUT_SECONDS", "").strip()
    timeout_seconds = float(configured_timeout) if configured_timeout else 0.0
    return {
        "hungRunId": hung_run_id,
        "hungRunStatus": str(hung_run["status"]),
        "plannerStartedAt": hung["startedAt"].isoformat(),
        "plannerTerminalEventObserved": hung["finishedAt"] is not None,
        "observedOpenSecondsUntilRunTerminal": _seconds(
            hung["startedAt"],
            hung_run["updated_at"],
        ),
        "configuredProviderTimeoutSeconds": (
            timeout_seconds if timeout_seconds > 0 else None
        ),
        "comparisonRunId": comparison_run_id,
        "comparisonPlannerDurationSeconds": comparison["durationSeconds"],
        "comparisonProviderDurationMs": comparison["providerDurationMs"],
        "comparisonInputBytes": comparison["inputBytes"],
        "manualDiagnosticReplay": (
            {
                "durationSeconds": diagnostic_replay_seconds,
                "inputBytes": diagnostic_input_bytes,
                "provenance": "operator_diagnostic_replay_in_current_p9_session",
            }
            if diagnostic_replay_seconds is not None
            else None
        ),
        "classification": "provider_request_without_terminal_event_or_deadline",
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# P9 Case B latency diagnosis",
        "",
        "Durations below come from persisted dispatch and durable-call timestamps.",
        "They describe the observed runs; 480 seconds remains an acceptance ceiling,",
        "not a latency target or a root-cause explanation.",
        "",
    ]
    for label, profile in (
        ("True single Agent", report["singleAgent"]),
        ("Controlled multi Agent", report["controlledMultiAgent"]),
    ):
        lines.extend(
            [
                f"## {label}",
                "",
                f"- run: `{profile['runId']}`",
                f"- wall time: `{profile['wallSeconds']}s`",
                f"- provider serial-equivalent time: "
                f"`{profile['providerSerialEquivalentSeconds']}s`",
                f"- calls: `{json.dumps(profile['callCounts'], sort_keys=True)}`",
                "",
                "| Segment | Seconds |",
                "| --- | ---: |",
            ]
        )
        lines.extend(
            f"| {segment['name']} | {segment['durationSeconds']} |"
            for segment in profile["segments"]
        )
        lines.extend(["", "Provider failures:"])
        if profile["providerFailures"]:
            lines.extend(
                "- "
                + f"`{failure['operationName']}` attempt "
                + f"`{failure['attemptNumber']}`: "
                + f"`{failure['failureCode']}` "
                + f"({failure['durationSeconds']}s)"
                for failure in profile["providerFailures"]
            )
        else:
            lines.append("- none")
        lines.append("")
    hang = report["plannerHangDiagnosis"]
    lines.extend(
        [
            "## Planner hang diagnosis",
            "",
            f"- hung run: `{hang['hungRunId']}`",
            f"- open request before terminal run recovery: "
            f"`{hang['observedOpenSecondsUntilRunTerminal']}s`",
            f"- planner terminal event observed: "
            f"`{str(hang['plannerTerminalEventObserved']).lower()}`",
            f"- configured provider timeout: "
            f"`{hang['configuredProviderTimeoutSeconds']}`",
            f"- later accepted DeepSeek planner call: "
            f"`{hang['comparisonPlannerDurationSeconds']}s`",
            f"- classification: `{hang['classification']}`",
            "",
            "## Diagnosis",
            "",
            "- The large wall time has separable causes: dispatch queue gaps, "
            "clarification/resume gaps, query and capability execution, semantic "
            "authority calls, controlled child retries, narrative generation, "
            "and the final persistence/delivery tail.",
            "- The hung planner run is a provider transport/request liveness failure: "
            "the durable attempt has `started` and no terminal event, and no positive "
            "provider timeout was configured.",
            "- Controlled multi Agent added a bounded critical path and a longer "
            "narrative input/output. The two child calls overlapped; their serial "
            "durations must not be added to estimate customer wait.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--single-run-id", required=True)
    parser.add_argument("--multi-run-id", required=True)
    parser.add_argument("--hung-run-id", required=True)
    parser.add_argument("--comparison-planner-run-id", required=True)
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--output-markdown", required=True, type=Path)
    parser.add_argument("--diagnostic-replay-seconds", type=float)
    parser.add_argument("--diagnostic-input-bytes", type=int)
    args = parser.parse_args()
    database_url = os.environ.get("WAJE_RUNTIME_DATABASE_URL")
    if not database_url:
        raise RuntimeError("runtime_database_url_required")
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        report = {
            "schemaVersion": "p9-case-b-latency-diagnosis.v1",
            "singleAgent": _run_profile(connection, args.single_run_id),
            "controlledMultiAgent": _run_profile(connection, args.multi_run_id),
            "plannerHangDiagnosis": _planner_hang_profile(
                connection,
                args.hung_run_id,
                args.comparison_planner_run_id,
                diagnostic_replay_seconds=args.diagnostic_replay_seconds,
                diagnostic_input_bytes=args.diagnostic_input_bytes,
            ),
        }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    args.output_markdown.write_text(_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "output_json": str(args.output_json),
                "output_markdown": str(args.output_markdown),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
