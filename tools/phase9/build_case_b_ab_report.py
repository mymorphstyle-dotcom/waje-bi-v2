from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import psycopg

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value


def _plan_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    axes = sorted(
        (
            item["axis_id"],
            item["axis_kind"],
            tuple(sorted(item.get("goal_refs", ()))),
            tuple(sorted(item.get("metric_refs", ()))),
            tuple(sorted(item.get("dimension_refs", ()))),
            tuple(sorted(item.get("capability_refs", ()))),
            item.get("selection_policy"),
            item.get("reconciliation_group"),
        )
        for item in payload["analysis_axes"]
    )
    tasks = sorted(
        (
            item["task_key"],
            item["capability_id"],
            tuple(sorted(item.get("metric_refs", ()))),
            tuple(sorted(item.get("dimension_refs", ()))),
            tuple(sorted(item.get("source_refs", ()))),
        )
        for item in payload["capability_tasks"]
    )
    obligations = {
        json.dumps(
            canonical_value(
                {
                    "role": item["role"],
                    "subject": {
                        key: value
                        for key, value in item["subject"].items()
                        if key
                        not in {
                            "planner_proposal_ref",
                            "proposal_item_ref",
                        }
                    },
                    "claim_kind": item["claim_kind"],
                    "success_policy": item["success_policy"],
                    "evidence_requirement": item["evidence_requirement"],
                }
            ),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for item in payload["claim_obligations"]
    }
    temporal = {
        key: value
        for key, value in payload["temporal_authority"].items()
        if key
        not in {
            "authority_ref",
            "content_digest",
            "decision_id",
        }
    }
    return canonical_value(
        {
            "analysis_axes": axes,
            "capability_tasks": tasks,
            "claim_obligations": sorted(obligations),
            "contract_versions": payload["contract_versions"],
            "temporal_authority": temporal,
            "context_window_specs": payload["context_window_specs"],
            "resolved_window_refs": payload["resolved_window_refs"],
            "budget_policy_ref": payload["budget_policy_ref"],
        }
    )


def _executable_plan_contract(
    plan_contract: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        key: value
        for key, value in plan_contract.items()
        if key != "claim_obligations"
    }


def _count(connection: Any, sql: str, run_id: str) -> int:
    return int(connection.execute(sql, (run_id,)).fetchone()[0])


def _run_record(connection: Any, run_id: str) -> dict[str, Any]:
    run = connection.execute(
        """
        SELECT status, request, created_at, updated_at
        FROM waje_runtime.analysis_runs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"run_missing:{run_id}")
    plan = connection.execute(
        """
        SELECT plan_revision_id, payload
        FROM waje_runtime.plan_revisions
        WHERE run_attempt_id = %s
          AND NOT EXISTS (
            SELECT 1
            FROM waje_runtime.plan_revision_supersessions supersession
            WHERE supersession.superseded_plan_revision_id
              = plan_revisions.plan_revision_id
          )
        """,
        (run_id,),
    ).fetchone()
    if plan is None:
        raise ValueError(f"accepted_plan_missing:{run_id}")
    plan_contract = _plan_contract(plan[1])
    provider_rows = connection.execute(
        """
        SELECT attempt.call_kind, attempt.operation_name,
               event.status, event.failure_code,
               COALESCE(event.output_payload, event.failure_payload)
        FROM waje_runtime.durable_call_attempts attempt
        JOIN waje_runtime.durable_call_attempt_events event
          ON event.attempt_ref = attempt.attempt_ref
        WHERE attempt.run_attempt_id = %s
          AND event.status IN ('succeeded', 'failed')
        ORDER BY event.created_at
        """,
        (run_id,),
    ).fetchall()
    providers: list[dict[str, Any]] = []
    for call_kind, operation_name, status, failure_code, payload in provider_rows:
        if not str(call_kind).endswith("_provider"):
            continue
        audit = payload.get("audit", {}) if isinstance(payload, Mapping) else {}
        providers.append(
            {
                "callKind": call_kind,
                "operationName": operation_name,
                "status": status,
                "failureCode": failure_code,
                "provider": audit.get("provider"),
                "model": audit.get("model"),
                "durationMs": audit.get("duration_ms"),
                "inputBytes": audit.get("input_bytes"),
                "outputBytes": audit.get("output_bytes"),
                "attemptCount": audit.get("attempt_count"),
                "baseUrlHash": audit.get("base_url_hash"),
            }
        )
    children = connection.execute(
        """
        SELECT dispatch_state, terminal_status, lease_epoch,
               failure_code, accepted_attempt_ref, accepted_artifact_ref,
               output_digest
        FROM waje_runtime.controlled_investigation_dispatches
        WHERE run_attempt_id = %s
        ORDER BY created_at, investigation_ref
        """,
        (run_id,),
    ).fetchall()
    request = run[1] if isinstance(run[1], Mapping) else {}
    created_at: datetime = run[2]
    updated_at: datetime = run[3]
    return {
        "runId": run_id,
        "status": run[0],
        "controlledInvestigationEnabled": bool(
            request.get("controlled_investigation_enabled")
        ),
        "durationSeconds": round((updated_at - created_at).total_seconds(), 3),
        "planRevisionId": plan[0],
        "planPayloadDigest": canonical_digest(plan[1]),
        "normalizedPlanDigest": canonical_digest(plan_contract),
        "normalizedPlanContract": plan_contract,
        "executablePlanDigest": canonical_digest(
            _executable_plan_contract(plan_contract)
        ),
        "counts": {
            "queries": _count(
                connection,
                """
                SELECT count(*)
                FROM waje_runtime.durable_call_attempts
                WHERE run_attempt_id = %s AND call_kind = 'query'
                """,
                run_id,
            ),
            "acceptedTasks": len(plan[1]["capability_tasks"]),
            "evidenceEntries": _count(
                connection,
                """
                SELECT count(*)
                FROM waje_runtime.capability_evidence_ledger_entries
                WHERE run_attempt_id = %s
                """,
                run_id,
            ),
            "verifiedClaims": _count(
                connection,
                """
                SELECT count(*)
                FROM waje_runtime.claim_revisions
                WHERE run_attempt_id = %s AND claim_status = 'verified'
                """,
                run_id,
            ),
            "narratives": _count(
                connection,
                """
                SELECT count(*)
                FROM waje_runtime.narrative_documents
                WHERE run_attempt_id = %s
                """,
                run_id,
            ),
            "publications": _count(
                connection,
                """
                SELECT count(*)
                FROM waje_runtime.publication_revisions
                WHERE run_attempt_id = %s
                """,
                run_id,
            ),
            "customerPayloads": _count(
                connection,
                """
                SELECT count(*)
                FROM waje_runtime.publication_customer_payloads
                WHERE run_attempt_id = %s
                """,
                run_id,
            ),
            "children": len(children),
        },
        "providerCalls": providers,
        "providerFailureCounts": dict(
            Counter(
                str(item["failureCode"])
                for item in providers
                if item["failureCode"]
            )
        ),
        "children": [
            {
                "dispatchState": item[0],
                "terminalStatus": item[1],
                "leaseEpoch": item[2],
                "failureCode": item[3],
                "acceptedAttemptRef": item[4],
                "acceptedArtifactRef": item[5],
                "outputDigest": item[6],
            }
            for item in children
        ],
    }


def _hard_contracts(
    single: Mapping[str, Any],
    multi: Mapping[str, Any],
    *,
    same_accepted_plan: bool,
    same_executable_plan: bool,
) -> dict[str, bool]:
    single_counts = single["counts"]
    multi_counts = multi["counts"]
    child_count = int(multi_counts["children"])
    deepseek_calls = tuple(single["providerCalls"]) + tuple(
        multi["providerCalls"]
    )
    return {
        "single_mode_disabled": (
            not single["controlledInvestigationEnabled"]
        ),
        "single_has_no_children": single_counts["children"] == 0,
        "multi_mode_enabled": bool(multi["controlledInvestigationEnabled"]),
        "multi_child_count_bounded": 1 <= child_count <= 3,
        "same_accepted_plan": same_accepted_plan,
        "same_executable_plan": same_executable_plan,
        "same_query_count": (
            single_counts["queries"] == multi_counts["queries"]
        ),
        "same_accepted_task_count": (
            single_counts["acceptedTasks"]
            == multi_counts["acceptedTasks"]
        ),
        "same_evidence_count": (
            single_counts["evidenceEntries"]
            == multi_counts["evidenceEntries"]
        ),
        "same_verified_claim_count": (
            single_counts["verifiedClaims"]
            == multi_counts["verifiedClaims"]
        ),
        "single_unique_delivery_chain": all(
            single_counts[key] == 1
            for key in ("narratives", "publications", "customerPayloads")
        ),
        "multi_unique_delivery_chain": all(
            multi_counts[key] == 1
            for key in ("narratives", "publications", "customerPayloads")
        ),
        "multi_children_source_closed": (
            child_count > 0
            and all(
                child["dispatchState"] == "terminal"
                and child["terminalStatus"] in {"accepted", "limited"}
                and bool(child["acceptedAttemptRef"])
                and bool(child["acceptedArtifactRef"])
                and bool(child["outputDigest"])
                for child in multi["children"]
            )
        ),
        "deepseek_only": (
            bool(deepseek_calls)
            and all(
                call["provider"] == "deepseek"
                and str(call["model"]).startswith("deepseek-")
                for call in deepseek_calls
            )
        ),
    }


def _markdown(report: Mapping[str, Any]) -> str:
    single = report["singleAgent"]
    multi = report["controlledMultiAgent"]
    return "\n".join(
        [
            "# P9 Case B A/B comparison",
            "",
            f"- hard-contract status: `{report['status']}`",
            f"- accepted Plan semantic contract equal: "
            f"`{report['sameAcceptedPlan']}`",
            f"- executable Plan contract equal: "
            f"`{report['sameExecutablePlan']}`",
            f"- single run: `{single['runId']}`",
            f"- controlled run: `{multi['runId']}`",
            f"- query counts: `{single['counts']['queries']}` / "
            f"`{multi['counts']['queries']}`",
            f"- Provider terminal calls: `{len(single['providerCalls'])}` / "
            f"`{len(multi['providerCalls'])}`",
            f"- durations: `{single['durationSeconds']}s` / "
            f"`{multi['durationSeconds']}s`",
            f"- controlled children: `{multi['counts']['children']}`",
            f"- narrative/publication/customer payload: "
            f"`{multi['counts']['narratives']}` / "
            f"`{multi['counts']['publications']}` / "
            f"`{multi['counts']['customerPayloads']}`",
            "",
            "## Hard contracts",
            "",
            *[
                f"- {name}: `{passed}`"
                for name, passed in report["hardContracts"].items()
            ],
            "",
            "Business-depth comparison and human advisory notes are recorded "
            "in the P9 implementation review. They do not change this hard "
            "authority report.",
            "",
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--single-run-id", required=True)
    parser.add_argument("--multi-run-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    database_url = os.getenv("WAJE_RUNTIME_DATABASE_URL")
    if not database_url:
        raise ValueError("runtime_database_url_missing")
    with psycopg.connect(database_url) as connection:
        single = _run_record(connection, args.single_run_id)
        multi = _run_record(connection, args.multi_run_id)
    same_accepted_plan = (
        single["normalizedPlanDigest"] == multi["normalizedPlanDigest"]
    )
    same_executable_plan = (
        single["executablePlanDigest"] == multi["executablePlanDigest"]
    )
    hard_contracts = _hard_contracts(
        single,
        multi,
        same_accepted_plan=same_accepted_plan,
        same_executable_plan=same_executable_plan,
    )
    status = "passed" if all(hard_contracts.values()) else "failed"
    report = {
        "schemaVersion": "p9-case-b-ab-comparison.v3",
        "status": status,
        "singleAgent": single,
        "controlledMultiAgent": multi,
        "sameAcceptedPlan": same_accepted_plan,
        "sameExecutablePlan": same_executable_plan,
        "hardContracts": hard_contracts,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    output.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")
    print(json.dumps({
        "status": status,
        "output": str(output),
        "sameAcceptedPlan": report["sameAcceptedPlan"],
        "sameExecutablePlan": report["sameExecutablePlan"],
    }))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
