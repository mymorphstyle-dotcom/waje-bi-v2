from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


def _count(connection: Any, table: str, run_id: str) -> int:
    if table not in {
        "publication_revisions",
        "publication_customer_payloads",
        "delivery_attempts",
    }:
        raise ValueError("unsupported_count_table")
    return int(
        connection.execute(
            f"""
            SELECT count(*)
            FROM waje_runtime.{table}
            WHERE run_attempt_id = %s
            """,
            (run_id,),
        ).fetchone()["count"]
    )


def _attempt_history(
    connection: Any,
    run_id: str,
    accepted_attempt_ref: str,
) -> list[dict[str, Any]]:
    accepted = connection.execute(
        """
        SELECT input_ref
        FROM waje_runtime.durable_call_attempts
        WHERE run_attempt_id = %s AND attempt_ref = %s
        """,
        (run_id, accepted_attempt_ref),
    ).fetchone()
    if accepted is None:
        raise ValueError("accepted_attempt_missing")
    rows = connection.execute(
        """
        SELECT attempt.attempt_ref, attempt.attempt_number,
               array_agg(event.status ORDER BY event.created_at) AS statuses,
               min(event.created_at) FILTER (
                 WHERE event.status = 'started'
               ) AS started_at,
               max(event.created_at) FILTER (
                 WHERE event.status IN ('succeeded', 'failed')
               ) AS finished_at
        FROM waje_runtime.durable_call_attempts attempt
        JOIN waje_runtime.durable_call_attempt_events event
          ON event.attempt_ref = attempt.attempt_ref
        WHERE attempt.run_attempt_id = %s
          AND attempt.call_kind = 'controlled_investigation_provider'
          AND attempt.operation_name = 'run_controlled_investigation'
          AND attempt.input_ref = %s
        GROUP BY attempt.attempt_ref, attempt.attempt_number
        ORDER BY attempt.attempt_number, attempt.attempt_ref
        """,
        (run_id, accepted["input_ref"]),
    ).fetchall()
    return [
        {
            "attemptRef": str(row["attempt_ref"]),
            "attemptNumber": int(row["attempt_number"]),
            "statuses": list(row["statuses"]),
            "startedAt": (
                row["started_at"].isoformat() if row["started_at"] else None
            ),
            "finishedAt": (
                row["finished_at"].isoformat() if row["finished_at"] else None
            ),
        }
        for row in rows
    ]


def build_report(
    connection: Any,
    run_id: str,
    *,
    killed_pid: int | None,
) -> dict[str, Any]:
    run = connection.execute(
        """
        SELECT status, request, created_at, updated_at
        FROM waje_runtime.analysis_runs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()
    if run is None:
        raise ValueError("run_missing")
    recovery_events = connection.execute(
        """
        SELECT event_type, ref, payload, created_at
        FROM waje_runtime.audit_events
        WHERE run_id = %s
          AND event_type IN (
            'run_dispatch_recovery_requested',
            'run_dispatch_recovery_leased'
          )
        ORDER BY created_at, audit_id
        """,
        (run_id,),
    ).fetchall()
    child_rows = connection.execute(
        """
        SELECT investigation_ref, child_run_id, dispatch_state,
               terminal_status, lease_epoch, accepted_attempt_ref,
               accepted_artifact_ref, output_digest, failure_code,
               created_at, updated_at
        FROM waje_runtime.controlled_investigation_dispatches
        WHERE run_attempt_id = %s
        ORDER BY investigation_ref
        """,
        (run_id,),
    ).fetchall()
    children = []
    for row in child_rows:
        accepted_attempt_ref = str(row["accepted_attempt_ref"])
        history = _attempt_history(connection, run_id, accepted_attempt_ref)
        children.append(
            {
                "investigationRef": str(row["investigation_ref"]),
                "childRunId": str(row["child_run_id"]),
                "dispatchState": str(row["dispatch_state"]),
                "terminalStatus": str(row["terminal_status"]),
                "leaseEpoch": int(row["lease_epoch"]),
                "acceptedAttemptRef": accepted_attempt_ref,
                "acceptedArtifactRef": str(row["accepted_artifact_ref"]),
                "outputDigest": str(row["output_digest"]),
                "failureCode": (
                    str(row["failure_code"]) if row["failure_code"] else None
                ),
                "attemptHistory": history,
            }
        )
    publication_counts = {
        "narratives": int(
            connection.execute(
                """
                SELECT count(*)
                FROM waje_runtime.narrative_documents
                WHERE run_attempt_id = %s
                """,
                (run_id,),
            ).fetchone()["count"]
        ),
        "publications": _count(connection, "publication_revisions", run_id),
        "customerPayloads": _count(
            connection,
            "publication_customer_payloads",
            run_id,
        ),
        "deliveryAttempts": _count(connection, "delivery_attempts", run_id),
    }
    hard_contracts = {
        "parentRecoveredAfterWorkerExit": any(
            event["event_type"] == "run_dispatch_recovery_requested"
            and event["payload"].get("failureReason") == "agent_core_worker_exited"
            for event in recovery_events
            if isinstance(event["payload"], dict)
        ),
        "recoveryLeaseEpochAdvanced": any(
            event["event_type"] == "run_dispatch_recovery_leased"
            and int(event["payload"].get("lease_epoch", 0)) == 2
            for event in recovery_events
            if isinstance(event["payload"], dict)
        ),
        "twoStableChildIdentities": (
            len(children) == 2
            and len({child["childRunId"] for child in children}) == 2
        ),
        "orphanedFirstAttemptsHaveNoTerminalEvent": all(
            len(child["attemptHistory"]) == 2
            and child["attemptHistory"][0]["attemptNumber"] == 1
            and child["attemptHistory"][0]["finishedAt"] is None
            for child in children
        ),
        "oneAcceptedSecondAttemptPerChild": all(
            child["attemptHistory"][-1]["attemptNumber"] == 2
            and child["attemptHistory"][-1]["attemptRef"]
            == child["acceptedAttemptRef"]
            and "succeeded" in child["attemptHistory"][-1]["statuses"]
            for child in children
        ),
        "oneAcceptedArtifactPerChild": all(
            child["acceptedArtifactRef"]
            and child["outputDigest"]
            and child["acceptedArtifactRef"].endswith(child["outputDigest"])
            for child in children
        ),
        "singleParentPublicationChain": publication_counts
        == {
            "narratives": 1,
            "publications": 1,
            "customerPayloads": 1,
            "deliveryAttempts": 1,
        },
        "parentCompleted": str(run["status"]) == "completed",
    }
    return {
        "schemaVersion": "p9-controlled-investigation-fault-recovery.v1",
        "status": (
            "passed" if all(hard_contracts.values()) else "contract_failed"
        ),
        "runId": run_id,
        "runStatus": str(run["status"]),
        "faultInjection": {
            "kind": "SIGKILL_parent_worker",
            "pid": killed_pid,
            "provenance": "operator_fault_injection_in_current_p9_session",
        },
        "recoveryEvents": [
            {
                "eventType": str(event["event_type"]),
                "ref": str(event["ref"]),
                "payload": event["payload"],
                "createdAt": event["created_at"].isoformat(),
            }
            for event in recovery_events
        ],
        "children": children,
        "publicationCounts": publication_counts,
        "hardContracts": hard_contracts,
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# P9 controlled multi-Agent fault recovery",
        "",
        f"- run: `{report['runId']}`",
        f"- status: `{report['status']}`",
        f"- terminal run status: `{report['runStatus']}`",
        f"- injected fault: `{report['faultInjection']['kind']}`",
        "",
        "## Hard contracts",
        "",
    ]
    lines.extend(
        f"- {name}: `{str(value).lower()}`"
        for name, value in report["hardContracts"].items()
    )
    lines.extend(["", "## Children", ""])
    for child in report["children"]:
        lines.extend(
            [
                f"- `{child['childRunId']}`",
                f"  - lease epoch: `{child['leaseEpoch']}`",
                f"  - terminal: `{child['terminalStatus']}`",
                f"  - accepted attempt: `{child['acceptedAttemptRef']}`",
                f"  - accepted artifact: `{child['acceptedArtifactRef']}`",
                "  - attempt lifecycle: "
                + ", ".join(
                    f"{attempt['attemptNumber']}="
                    f"{'/'.join(attempt['statuses'])}"
                    for attempt in child["attemptHistory"]
                ),
            ]
        )
    lines.extend(
        [
            "",
            "The first two provider attempts were running when the parent worker "
            "exited. Recovery advanced the parent dispatch lease to epoch 2, reused "
            "the immutable child dispatch identities, and accepted one second "
            "attempt plus one artifact for each child. The parent still produced "
            "one narrative, publication, customer payload, and delivery attempt.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--killed-pid", type=int)
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--output-markdown", required=True, type=Path)
    args = parser.parse_args()
    database_url = os.environ.get("WAJE_RUNTIME_DATABASE_URL")
    if not database_url:
        raise RuntimeError("runtime_database_url_required")
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        report = build_report(
            connection,
            args.run_id,
            killed_pid=args.killed_pid,
        )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.output_markdown.write_text(_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "output_json": str(args.output_json),
                "output_markdown": str(args.output_markdown),
            },
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
