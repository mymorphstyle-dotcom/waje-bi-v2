from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap

ROOT = Path(__file__).resolve().parents[2]
GATEWAY_SOURCES = (
    "app/api/runs/[runId]/clarifications/route.ts",
    "app/api/threads/[threadId]/messages/route.ts",
    "app/api/_agentCore.ts",
    "app/api/_pythonRuntime.ts",
    "app/api/_conversationStore.ts",
)


def test_shared_run_dispatch_outbox_replays_conflicts_and_fences_stale_owner():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            const thread = {
              id: "thread-shared-dispatch",
              ownerId: "local-user",
              topicIds: [],
              messages: [],
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map(),
              memoryProposals: new Map(),
              clarificationResolutions: new Map(),
              clarificationExecutionAttempts: new Map(),
              runDispatches: new Map(),
              auditEvents: [],
            };
            const {
              acquireRunDispatchLease,
              claimRunDispatchRequest,
              failOwnedRunDispatch,
            } = require(out + "/app/api/_conversationStore.js");
            const input = {
              producerKind: "thread_message",
              scopeRef: thread.id,
              requestIdentity: "client-request-1",
              threadId: thread.id,
              text: "检查昨天收入",
              actorId: "local-user",
              requestPayload: { message: "检查昨天收入" },
            };
            (async () => {
              const claims = await Promise.all([
                claimRunDispatchRequest(input),
                claimRunDispatchRequest(input),
              ]);
              let conflict = null;
              try {
                await claimRunDispatchRequest({
                  ...input,
                  text: "检查今天收入",
                  requestPayload: { message: "检查今天收入" },
                });
              } catch (error) {
                conflict = { code: error.code, status: error.httpStatus };
              }
              const first = await acquireRunDispatchLease({
                dispatchId: claims[0].dispatch.dispatchId,
                runId: claims[0].run.id,
              });
              const contested = await acquireRunDispatchLease({
                dispatchId: claims[0].dispatch.dispatchId,
                runId: claims[0].run.id,
              });
              const dispatch = globalThis.__wajeConversationMemoryStore
                .runDispatches.get(claims[0].dispatch.dispatchId);
              dispatch.leaseExpiresAt = "2000-01-01T00:00:00.000Z";
              const replacement = await acquireRunDispatchLease({
                dispatchId: claims[0].dispatch.dispatchId,
                runId: claims[0].run.id,
              });
              const stale = await failOwnedRunDispatch({
                dispatchId: claims[0].dispatch.dispatchId,
                runId: claims[0].run.id,
                ownerId: first.ownerId,
                leaseEpoch: first.leaseEpoch,
                failureReason: "stale-owner",
              });
              const current = await failOwnedRunDispatch({
                dispatchId: claims[0].dispatch.dispatchId,
                runId: claims[0].run.id,
                ownerId: replacement.ownerId,
                leaseEpoch: replacement.leaseEpoch,
                failureReason: "current-owner",
              });
              const store = globalThis.__wajeConversationMemoryStore;
              console.log(JSON.stringify({
                claimRunIds: claims.map((claim) => claim.run.id),
                replayed: claims.map((claim) => claim.replayed),
                conflict,
                first,
                contested,
                replacement,
                staleStatus: stale.status,
                currentStatus: current.status,
                currentReason: current.request.failure_reason,
                messageCount: thread.messages.length,
                runCount: store.runs.size,
                dispatchCount: store.runDispatches.size,
                auditTypes: store.auditEvents.map((event) => event.eventType),
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
    )

    assert len(set(result["claimRunIds"])) == 1
    assert sorted(result["replayed"]) == [False, True]
    assert result["conflict"] == {"code": "run_dispatch_conflict", "status": 409}
    assert result["first"]["acquired"] is True
    assert result["contested"]["reason"] == "active_lease"
    assert result["replacement"]["acquired"] is True
    assert result["replacement"]["leaseEpoch"] > result["first"]["leaseEpoch"]
    assert result["staleStatus"] == "queued"
    assert result["currentStatus"] == "failed"
    assert result["currentReason"] == "current-owner"
    assert result["messageCount"] == 1
    assert result["runCount"] == 1
    assert result["dispatchCount"] == 1
    assert result["auditTypes"].count("message_recorded") == 1
    assert result["auditTypes"].count("run_queued") == 1


def test_customer_publication_contract_rejects_extra_fields_without_text_scanning():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            const { parseCustomerPublication } = require(
              out + "/app/api/_customerPublicationContract.js"
            );
            const valid = {
              blocks: [{
                role: "executive_answer",
                text: "owner_ref 是本次业务标签原文，允许作为正常表达。",
                statement_role: "conclusion",
                claim_refs: ["claim-one"],
                recommendation_refs: [],
                limitation_refs: [],
                material_fact_bindings: [{
                  fact_kind: "number",
                  name: "付费金额",
                  range_end: null,
                  unit: "CNY",
                  value: "120.5",
                }],
              }],
              claim_refs: ["claim-one"],
              field_visibility_policy_ref: "policy-customer",
              limitation_refs: [],
              recommendation_refs: [],
              visualization_refs: [],
              warnings: [],
            };
            const errors = [];
            for (const payload of [
              { ...valid, owner_ref: "owner-secret" },
              {
                ...valid,
                blocks: [{
                  ...valid.blocks[0],
                  raw_provider_response: "hidden",
                }],
              },
              {
                ...valid,
                blocks: [{
                  ...valid.blocks[0],
                  material_fact_bindings: [{
                    ...valid.blocks[0].material_fact_bindings[0],
                    raw_row: { player_id: 7 },
                  }],
                }],
              },
            ]) {
              try {
                parseCustomerPublication(payload);
              } catch (error) {
                errors.push(error.message);
              }
            }
            console.log(JSON.stringify({
              accepted: parseCustomerPublication(valid),
              errors,
            }));
            """
        ),
    )

    assert result["accepted"]["blocks"][0]["text"].startswith("owner_ref")
    assert result["errors"] == ["customer_publication_invalid"] * 3


def test_run_audit_trace_projects_only_current_authority_refs_and_safe_diagnostics():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            const timestamp = "2026-07-18T00:00:00.000Z";
            globalThis.__wajeConversationPool = {
              async query(statement) {
                if (statement.includes("JOIN waje_runtime.investigation_threads")) {
                  return { rows: [{
                    run_id: "run-safe-audit",
                    thread_id: "thread-safe-audit",
                    status: "evidence_ready",
                    request: { raw_provider_response: "must-not-leak" },
                    created_at: timestamp,
                    owner_id: "local-user",
                  }] };
                }
                if (statement.includes("SELECT run_id, status, request")) {
                  return { rows: [{
                    run_id: "run-safe-audit",
                    status: "evidence_ready",
                    request: {
                      owner_ref: "owner-secret",
                      raw_provider_response: "must-not-leak",
                    },
                    created_at: timestamp,
                    updated_at: timestamp,
                  }] };
                }
                if (statement.includes("FROM waje_runtime.run_nodes")) {
                  return { rows: [{
                    node_name: "execute_capability_dag",
                    status: "completed",
                    started_at: timestamp,
                    finished_at: timestamp,
                    payload: { raw_rows: [{ player_id: 7 }] },
                    owner_ref: "owner-secret",
                  }] };
                }
                if (statement.includes("capability_evidence_ledger_entries")) {
                  return { rows: [{
                    entry_ref: "ledger-entry-one",
                    evidence_ref: "evidence-one",
                    task_id: "task-one",
                    outcome_ref: "outcome-one",
                    binding_record_ref: "binding-one",
                    execution_state: "available",
                    evidence_kind: "observed",
                    data_contract_state: "supported",
                    maximum_claim_strength: "descriptive",
                    result_membership_digest: "a".repeat(64),
                    completeness_membership_digest: "b".repeat(64),
                    created_at: timestamp,
                    payload: { raw_row: { player_id: 7 } },
                  }] };
                }
                if (statement.includes("FROM waje_runtime.query_runs query_run")) {
                  return { rows: [{
                    result_ref: "result-one",
                    query_contract_ref: "query-contract-one",
                    analysis_contract_ref: "analysis-contract-one",
                    query_contract_signature: "c".repeat(64),
                    analysis_contract_signature: "d".repeat(64),
                    execution_status: "succeeded",
                    query_hash: "e".repeat(64),
                    completeness_report_ref: "completeness-one",
                    query_record_ref: "query-record-one",
                    query_record_digest: "f".repeat(64),
                    completeness_record_ref: "completeness-record-one",
                    completeness_digest: "1".repeat(64),
                    completeness_status: "complete",
                    analysis_readiness: "ready",
                    row_count: "12",
                    snapshot_refs: ["snapshot-one"],
                    created_at: timestamp,
                    raw_provider_response: "must-not-leak",
                    raw_rows: [{ player_id: 7 }],
                  }] };
                }
                if (statement.includes("capability_execution_snapshots")) {
                  return { rows: [{
                    execution_snapshot_ref: "execution-snapshot-one",
                    authority_context_ref: "authority-context-one",
                    plan_revision_id: "plan-one",
                    stop_ref: "stop-one",
                    outcome_set_digest: "2".repeat(64),
                    evidence_ledger_digest: "3".repeat(64),
                    content_digest: "4".repeat(64),
                    created_at: timestamp,
                    owner_ref: "owner-secret",
                  }] };
                }
                if (statement.includes("FROM waje_runtime.audit_events")) {
                  return { rows: [{
                    event_type: "execution_result_ready",
                    actor_id: "internal-actor",
                    payload: {
                      status: "evidence_ready",
                      owner_ref: "owner-secret",
                      raw_provider_response: "must-not-leak",
                    },
                    created_at: timestamp,
                  }] };
                }
                if (statement.includes("claim_verification_decisions")) {
                  return { rows: [{
                    accepted_claim_count: "1",
                    vetoed_claim_count: "0",
                    vetoed_block_count: "0",
                    claim_report_refs: ["claim-report-one"],
                    block_report_refs: ["block-report-one"],
                    owner_ref: "owner-secret",
                  }] };
                }
                throw new Error("unexpected_query:" + statement);
              },
            };
            const { runAuditTrace } = require(
              out + "/app/api/_conversationStore.js"
            );
            (async () => {
              const trace = await runAuditTrace("run-safe-audit", "local-user");
              console.log(JSON.stringify({ trace, serialized: JSON.stringify(trace) }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        env={"WAJE_RUNTIME_DATABASE_URL": "postgres://safe-audit"},
    )

    trace = result["trace"]
    assert set(trace["run"]) == {"run_id", "status", "created_at", "updated_at"}
    assert set(trace["runNodes"][0]) == {
        "node_name",
        "status",
        "started_at",
        "finished_at",
    }
    assert trace["traceCompleteness"] == {
        "hasCustomerPublication": False,
        "evidenceRefCount": 1,
        "resultRefCount": 1,
        "contractRefs": ["query-contract-one", "analysis-contract-one"],
        "snapshotRefs": ["snapshot-one"],
        "queryRefs": ["query-contract-one"],
        "resultRefs": ["result-one"],
    }
    assert trace["auditEvents"] == [
        {
            "event_type": "execution_result_ready",
            "created_at": "2026-07-18T00:00:00.000Z",
            "diagnostic": {"status": "evidence_ready"},
        }
    ]
    for forbidden in (
        "owner_ref",
        "owner-secret",
        "actor_id",
        "internal-actor",
        "raw_provider_response",
        "raw_rows",
        "raw_row",
        "player_id",
    ):
        assert forbidden not in result["serialized"]


def _compiled_gateway_run(
    source: str,
    *,
    fake_python: str | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="waje-gateway-runtime-") as tmp:
        out_dir = Path(tmp) / "compiled"
        compile_result = subprocess.run(
            [
                "npx",
                "tsc",
                "--ignoreConfig",
                "--noEmit",
                "false",
                "--module",
                "node16",
                "--moduleResolution",
                "node16",
                "--target",
                "ES2022",
                "--esModuleInterop",
                "--skipLibCheck",
                "--outDir",
                str(out_dir),
                "--rootDir",
                str(ROOT),
                *GATEWAY_SOURCES,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert compile_result.returncode == 0, (
            compile_result.stdout + compile_result.stderr
        )

        explicit_env = dict(env or {})
        process_env = {
            **os.environ,
            "NODE_PATH": str(ROOT / "node_modules"),
            "GATEWAY_OUT": str(out_dir),
            "WAJE_GATEWAY_TEST_TMP": tmp,
            "NODE_ENV": "test",
            "WAJE_GATEWAY_UNIT_TEST_STORE": "memory",
        }
        if not any(
            key in explicit_env for key in ("WAJE_RUNTIME_DATABASE_URL", "DATABASE_URL")
        ):
            process_env.pop("WAJE_RUNTIME_DATABASE_URL", None)
            process_env.pop("DATABASE_URL", None)
        if fake_python is not None:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            executable = bin_dir / "uv"
            executable.write_text(
                fake_python.replace(
                    "#!/usr/bin/env python3",
                    f"#!{sys.executable}",
                    1,
                ),
                encoding="utf-8",
            )
            executable.chmod(
                executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
            process_env["PATH"] = f"{bin_dir}:{process_env['PATH']}"
        process_env.update(explicit_env)
        result = subprocess.run(
            [shutil.which("node") or "node", "-e", source],
            cwd=ROOT,
            env=process_env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1])


def _successful_agent_core() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import json
        import os
        import sys

        counter = os.path.join(os.environ["WAJE_GATEWAY_TEST_TMP"], "agent-invocations")
        with open(counter, "a", encoding="utf-8") as handle:
            handle.write("1\\n")
        run_id = sys.argv[sys.argv.index("--run-id") + 1]
        print(json.dumps({
            "status": "completed",
            "run_id": run_id,
            "turn_id": "turn-gateway-runtime",
            "topic_id": "topic-gateway-runtime",
            "context_manifest": {"manifest_id": "manifest-gateway-runtime"},
            "customer_publication": {
                "blocks": [{
                    "role": "executive_answer",
                    "text": "运行已发布。",
                    "statement_role": "conclusion",
                    "claim_refs": [],
                    "recommendation_refs": [],
                    "limitation_refs": [],
                    "material_fact_bindings": [],
                }],
                "claim_refs": [],
                "field_visibility_policy_ref": "policy-gateway-runtime",
                "limitation_refs": [],
                "recommendation_refs": [],
                "visualization_refs": [],
                "warnings": [],
            },
            "publication": {
                "authority_bundle_ref": "bundle-gateway-runtime",
                "authority_bundle_digest": "a" * 64,
                "publication_ref": "publication-gateway-runtime",
                "publication_digest": "b" * 64,
                "projection_id": "projection-gateway-runtime",
                "projection_digest": "c" * 64,
                "outbox_ref": "outbox-gateway-runtime",
                "delivery_status": "published",
            },
        }))
        """
    )


def test_detached_agent_core_requires_authoritative_startup_acknowledgment():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            const { runAgentCore } = require(out + "/app/api/_agentCore.js");
            (async () => {
              const result = await runAgentCore(
                "thread-startup",
                "run-startup",
                "检查付费金额",
                "local-user",
              );
              console.log(JSON.stringify(result));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        fake_python=textwrap.dedent(
            """\
            #!/usr/bin/env python3
            raise SystemExit(0)
            """
        ),
    )

    assert result["status"] == "failed"
    assert result["error"] == "agent_core_startup_failed"


def test_detached_agent_core_startup_lease_expires_without_acknowledgment():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            const fs = require("fs");
            const path = require("path");
            const { runAgentCore } = require(out + "/app/api/_agentCore.js");
            (async () => {
              const result = await runAgentCore(
                "thread-startup-lease",
                "run-startup-lease",
                "检查付费金额",
                "local-user",
              );
              const sentinel = path.join(
                process.env.WAJE_GATEWAY_TEST_TMP,
                "survived-timeout",
              );
              const deadline = Date.now() + 5000;
              while (!fs.existsSync(sentinel) && Date.now() < deadline) {
                await new Promise((resolve) => setTimeout(resolve, 25));
              }
              const survived = fs.existsSync(sentinel);
              console.log(JSON.stringify({ ...result, survived }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        fake_python=textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            import time

            time.sleep(0.1)
            with open(
                os.path.join(os.environ["WAJE_GATEWAY_TEST_TMP"], "survived-timeout"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write("running-owner-survived")
            os.write(
                int(os.environ["WAJE_AGENT_CORE_STARTUP_ACK_FD"]),
                b"WAJE_AGENT_CORE_RUNNING\\n",
            )
            """
        ),
        env={"WAJE_AGENT_CORE_STARTUP_ACK_TIMEOUT_MS": "50"},
    )

    assert result["status"] == "failed"
    assert result["error"] == "agent_core_startup_failed"
    assert result["survived"] is True


def test_topic_selection_is_exact_persisted_and_forwarded_to_agent_core():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const fs = require("fs");
            const path = require("path");
            const out = process.env.GATEWAY_OUT;
            const thread = {
              id: "thread-topic-choice",
              ownerId: "local-user",
              topicIds: ["topic-revenue", "topic-retention"],
              messages: [],
              createdAt: "2026-07-18T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map(),
              memoryProposals: new Map(),
              clarificationResolutions: new Map(),
              clarificationExecutionAttempts: new Map(),
              runDispatches: new Map(),
              auditEvents: [],
            };
            const { POST } = require(
              out + "/app/api/threads/[threadId]/messages/route.js"
            );
            const request = (identity, body) => new NextRequest(
              "http://localhost/api/threads/thread-topic-choice/messages",
              {
                method: "POST",
                headers: {
                  "content-type": "application/json",
                  "idempotency-key": identity,
                },
                body: JSON.stringify(body),
              },
            );
            (async () => {
              const response = await POST(
                request("topic-choice-valid", {
                  message: "收入变化",
                  topicSelection: {
                    sourceRunId: "run-topic-choice-source",
                    topicId: "topic-revenue",
                  },
                }),
                { params: Promise.resolve({ threadId: thread.id }) },
              );
              const responseBody = await response.json();
              const freeTextResponse = await POST(
                request("topic-choice-free-text", {
                  message: "继续看近期收入波动那个主题",
                  topicChoiceAnswer: {
                    sourceRunId: "run-topic-choice-source",
                    answer: "继续看近期收入波动那个主题",
                  },
                }),
                { params: Promise.resolve({ threadId: thread.id }) },
              );
              const freeTextBody = await freeTextResponse.json();
              const invalid = await POST(
                request("topic-choice-invalid", {
                  message: "收入变化",
                  topicSelection: {
                    sourceRunId: "run-topic-choice-source",
                    topicId: "topic-revenue",
                    label: "must-not-be-forwarded",
                  },
                }),
                { params: Promise.resolve({ threadId: thread.id }) },
              );
              const mismatch = await POST(
                request("topic-choice-answer-mismatch", {
                  message: "继续收入主题",
                  topicChoiceAnswer: {
                    sourceRunId: "run-topic-choice-source",
                    answer: "继续留存主题",
                  },
                }),
                { params: Promise.resolve({ threadId: thread.id }) },
              );
              const conflict = await POST(
                request("topic-choice-input-conflict", {
                  message: "收入变化",
                  topicSelection: {
                    sourceRunId: "run-topic-choice-source",
                    topicId: "topic-revenue",
                  },
                  topicChoiceAnswer: {
                    sourceRunId: "run-topic-choice-source",
                    answer: "收入变化",
                  },
                }),
                { params: Promise.resolve({ threadId: thread.id }) },
              );
              const unknownField = await POST(
                request("topic-choice-unknown-field", {
                  message: "收入变化",
                  intentRevisionContext: {
                    supersedes_intent_revision_id: "client-injection",
                  },
                }),
                { params: Promise.resolve({ threadId: thread.id }) },
              );
              const store = globalThis.__wajeConversationMemoryStore;
              const dispatch = [...store.runDispatches.values()].find(
                (item) => item.requestIdentity === "topic-choice-valid"
              );
              const freeTextDispatch = [...store.runDispatches.values()].find(
                (item) => item.requestIdentity === "topic-choice-free-text"
              );
              const argvRecords = fs.readFileSync(
                path.join(process.env.WAJE_GATEWAY_TEST_TMP, "topic-choice-argv.json"),
                "utf-8",
              ).trim().split("\\n").map(JSON.parse);
              const selectionArgv = argvRecords.find(
                (argv) => argv.includes("--topic-selection")
              );
              const answerArgv = argvRecords.find(
                (argv) => argv.includes("--topic-choice-answer")
              );
              const selectionIndex = selectionArgv.indexOf("--topic-selection");
              const answerIndex = answerArgv.indexOf("--topic-choice-answer");
              console.log(JSON.stringify({
                status: response.status,
                responseBody,
                freeTextStatus: freeTextResponse.status,
                invalidStatus: invalid.status,
                invalidBody: await invalid.json(),
                mismatchStatus: mismatch.status,
                mismatchBody: await mismatch.json(),
                conflictStatus: conflict.status,
                conflictBody: await conflict.json(),
                unknownFieldStatus: unknownField.status,
                unknownFieldBody: await unknownField.json(),
                requestPayload: dispatch.requestPayload,
                freeTextRequestPayload: freeTextDispatch.requestPayload,
                cliTopicSelection: JSON.parse(selectionArgv[selectionIndex + 1]),
                cliTopicChoiceAnswer: JSON.parse(answerArgv[answerIndex + 1]),
                invalidDispatchCreated: [...store.runDispatches.values()].some(
                  (item) => item.requestIdentity === "topic-choice-invalid"
                ),
                technicalErrors: store.auditEvents
                  .filter((item) => item.eventType === "customer_request_failed")
                  .map((item) => item.payload.internalCode),
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        fake_python=textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys

            with open(
                os.path.join(
                    os.environ["WAJE_GATEWAY_TEST_TMP"],
                    "topic-choice-argv.json",
                ),
                "a",
                encoding="utf-8",
            ) as handle:
                json.dump(sys.argv, handle)
                handle.write("\\n")
            run_id = sys.argv[sys.argv.index("--run-id") + 1]
            print(json.dumps({
                "status": "interaction_completed",
                "run_id": run_id,
                "turn_id": "turn-topic-choice",
                "topic_id": "topic-revenue",
                "intent": "follow_up",
                "topic_relation": "ask_topic_choice",
                "context_manifest": {"manifest_id": "context-topic-choice"},
                "interaction_result": {
                    "schema_version": "typed-topic-choice.v1",
                    "intent": "follow_up",
                    "response_text": "请选择要继续分析的主题。",
                    "options": [
                        {
                            "topic_id": "topic-revenue",
                            "label": "收入变化",
                            "description": "继续查看收入变化主题。",
                        },
                        {
                            "topic_id": "topic-retention",
                            "label": "留存变化",
                            "description": "继续查看留存变化主题。",
                        },
                    ],
                    "recommended_topic_id": "topic-revenue",
                    "allow_free_text": True,
                },
            }, ensure_ascii=False))
            """
        ),
        env={"WAJE_AGENT_CORE_INLINE": "1"},
    )

    selection = {
        "sourceRunId": "run-topic-choice-source",
        "topicId": "topic-revenue",
    }
    assert result["status"] == 202
    assert result["responseBody"]["snapshot"]["state"]["status"] == "needs_input"
    assert result["requestPayload"] == {
        "message": "收入变化",
        "topicSelection": selection,
    }
    choice_answer = {
        "sourceRunId": "run-topic-choice-source",
        "answer": "继续看近期收入波动那个主题",
    }
    assert result["freeTextStatus"] == 202
    assert result["freeTextRequestPayload"] == {
        "message": choice_answer["answer"],
        "topicChoiceAnswer": choice_answer,
    }
    assert result["cliTopicSelection"] == selection
    assert result["cliTopicChoiceAnswer"] == choice_answer
    assert result["invalidStatus"] == 400
    assert result["invalidBody"]["error"]["code"] == "request_invalid"
    assert result["mismatchStatus"] == 400
    assert result["mismatchBody"]["error"]["code"] == "request_invalid"
    assert result["conflictStatus"] == 400
    assert result["conflictBody"]["error"]["code"] == "request_invalid"
    assert result["unknownFieldStatus"] == 400
    assert result["unknownFieldBody"]["error"]["code"] == "request_invalid"
    assert result["invalidDispatchCreated"] is False
    assert set(result["technicalErrors"]) == {
        "topic_selection_invalid",
        "topic_choice_answer_message_mismatch",
        "topic_choice_input_conflict",
        "message_request_invalid",
    }


def test_detached_worker_exit_after_ack_terminalizes_only_owned_dispatch():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const out = process.env.GATEWAY_OUT;
            const thread = {
              id: "thread-post-ack-exit",
              ownerId: "local-user",
              topicIds: [],
              messages: [],
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map(),
              memoryProposals: new Map(),
              clarificationResolutions: new Map(),
              clarificationExecutionAttempts: new Map(),
              runDispatches: new Map(),
              auditEvents: [],
            };
            const { POST } = require(
              out + "/app/api/threads/[threadId]/messages/route.js"
            );
            (async () => {
              const response = await POST(
                new NextRequest(
                  "http://localhost/api/threads/thread-post-ack-exit/messages",
                  {
                    method: "POST",
                    headers: {
                      "content-type": "application/json",
                      "idempotency-key": "post-ack-exit-request",
                    },
                    body: JSON.stringify({ message: "检查付费金额" }),
                  },
                ),
                { params: Promise.resolve({ threadId: thread.id }) },
              );
              const body = await response.json();
              const store = globalThis.__wajeConversationMemoryStore;
              const runId = body.snapshot.transport.runHandle;
              const deadline = Date.now() + 2000;
              while (
                store.runs.get(runId)?.status !== "failed"
                && Date.now() < deadline
              ) {
                await new Promise((resolve) => setTimeout(resolve, 20));
              }
              const run = store.runs.get(runId);
              const dispatch = [...store.runDispatches.values()].find(
                (item) => item.requestIdentity === "post-ack-exit-request"
              );
              console.log(JSON.stringify({
                httpStatus: response.status,
                initialState: body.snapshot.state.status,
                run,
                dispatch,
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        fake_python=textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            import time

            os.write(
                int(os.environ["WAJE_AGENT_CORE_STARTUP_ACK_FD"]),
                b"WAJE_AGENT_CORE_RUNNING\\n",
            )
            time.sleep(0.05)
            raise SystemExit(7)
            """
        ),
    )

    assert result["httpStatus"] == 202
    assert result["initialState"] == "working"
    assert result["run"]["status"] == "failed"
    assert result["run"]["request"]["failure_reason"] == "agent_core_worker_exited"
    assert result["dispatch"]["state"] == "terminal"


def test_real_message_route_maps_known_startup_failure_to_503_and_terminalizes_queue():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const out = process.env.GATEWAY_OUT;
            const thread = {
              id: "thread-message-startup",
              ownerId: "local-user",
              topicIds: [],
              messages: [],
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map(),
              memoryProposals: new Map(),
              clarificationResolutions: new Map(),
              clarificationExecutionAttempts: new Map(),
              auditEvents: [],
            };
            const { POST } = require(
              out + "/app/api/threads/[threadId]/messages/route.js"
            );
            (async () => {
              const response = await POST(
                new NextRequest("http://localhost/api/threads/thread-message-startup/messages", {
                  method: "POST",
                  headers: {
                    "content-type": "application/json",
                    "idempotency-key": "message-startup-request",
                  },
                  body: JSON.stringify({ message: "检查付费金额" }),
                }),
                { params: Promise.resolve({ threadId: thread.id }) },
              );
              const store = globalThis.__wajeConversationMemoryStore;
              console.log(JSON.stringify({
                status: response.status,
                body: await response.json(),
                runs: [...store.runs.values()],
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        env={"PATH": "/nonexistent"},
    )

    assert result["status"] == 503
    assert result["body"]["error"]["code"] == "analysis_unavailable"
    assert result["body"]["transport"]["technicalDetailRef"]
    assert len(result["runs"]) == 1
    assert result["runs"][0]["status"] == "failed"
    assert result["runs"][0]["request"]["failure_reason"] == "agent_core_spawn_failed"


def test_gateway_postgres_created_queue_uses_runtime_store_contract():
    gateway = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            const state = { run: null, statements: [] };
            globalThis.__wajeConversationPool = {
                async query(statement, params = []) {
                  state.statements.push(statement);
                  if (statement.includes("FROM waje_runtime.investigation_threads")) {
                    return { rows: [{
                      thread_id: "thread-gateway-adapter",
                      owner_id: "local-user",
                      created_at: "2026-07-14T00:00:00.000Z",
                    }] };
                  }
                if (statement.includes("INSERT INTO waje_runtime.analysis_runs")) {
                  state.run = {
                    run_id: params[0],
                    thread_id: params[1],
                    status: params[2],
                    request: {},
                  };
                }
                return { rows: [] };
              },
            };
            const { createRun } = require(out + "/app/api/_conversationStore.js");
            (async () => {
                const run = await createRun("thread-gateway-adapter", "local-user");
              console.log(JSON.stringify({ run, state }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        env={"WAJE_RUNTIME_DATABASE_URL": "postgres://transaction-adapter"},
    )

    assert gateway["run"]["status"] == "queued"
    assert gateway["state"]["run"]["run_id"] == gateway["run"]["id"]
    assert any(
        "INSERT INTO waje_runtime.analysis_runs" in statement
        for statement in gateway["state"]["statements"]
    )
