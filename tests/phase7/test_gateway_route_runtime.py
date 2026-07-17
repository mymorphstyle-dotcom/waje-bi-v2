from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
ROOT = Path(__file__).resolve().parents[2]
GATEWAY_SOURCES = (
    "app/api/runs/[runId]/clarifications/route.ts",
    "app/api/threads/[threadId]/messages/route.ts",
    "app/api/artifacts/[artifactId]/continue/route.ts",
    "app/api/artifacts/[artifactId]/route.ts",
    "app/api/artifacts/[artifactId]/export/route.ts",
    "app/api/_agentCore.ts",
    "app/api/_conversationStore.ts",
)


def test_artifact_handlers_use_one_data_capability_and_preserve_thread_boundary():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const out = process.env.GATEWAY_OUT;
            const thread = {
              id: "thread-artifact",
              ownerId: "local-user",
              topicIds: ["topic-artifact"],
              messages: [],
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            const artifact = {
              id: "artifact-reviewed",
              threadId: thread.id,
              topicId: "topic-artifact",
              snapshotId: "snapshot-reviewed",
              followUpContext: "continue",
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map(),
              artifacts: new Map([[artifact.id, artifact]]),
              memoryProposals: new Map(),
              clarificationResolutions: new Map(),
              clarificationExecutionAttempts: new Map(),
              auditEvents: [],
            };
            const open = require(out + "/app/api/artifacts/[artifactId]/route.js");
            const exported = require(
              out + "/app/api/artifacts/[artifactId]/export/route.js"
            );
            const continued = require(
              out + "/app/api/artifacts/[artifactId]/continue/route.js"
            );
            const context = (artifactId) => ({
              params: Promise.resolve({ artifactId }),
            });
            (async () => {
              const missing = await open.GET(
                new Request("http://localhost/api/artifacts/missing"),
                context("missing"),
              );
              const opened = await open.GET(
                new Request("http://localhost/api/artifacts/artifact-reviewed"),
                context(artifact.id),
              );
              const exportedArtifact = await exported.GET(
                new Request("http://localhost/api/artifacts/artifact-reviewed/export"),
                context(artifact.id),
              );
              const mismatch = await continued.POST(
                new NextRequest(
                  "http://localhost/api/artifacts/artifact-reviewed/continue",
                  {
                    method: "POST",
                    headers: { "content-type": "application/json" },
                    body: JSON.stringify({
                      message: "继续",
                      threadId: "thread-other",
                    }),
                  },
                ),
                context(artifact.id),
              );
              const allowed = await open.GET(
                new Request("http://localhost/api/artifacts/artifact-reviewed"),
                context(artifact.id),
              );
              const store = globalThis.__wajeConversationMemoryStore;
              console.log(JSON.stringify({
                statuses: [
                  missing.status,
                  opened.status,
                  exportedArtifact.status,
                  mismatch.status,
                  allowed.status,
                ],
                messageCount: thread.messages.length,
                runCount: store.runs.size,
                auditTypes: store.auditEvents.map((event) => event.eventType),
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
    )

    assert result["statuses"] == [404, 200, 200, 409, 200]
    assert result["messageCount"] == 0
    assert result["runCount"] == 0
    assert "artifact_continue_blocked" not in result["auditTypes"]
    assert "artifact_opened" in result["auditTypes"]
    assert "artifact_exported" in result["auditTypes"]


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
              artifacts: new Map(),
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
                runId: claims[0].run.id,
                requestIdentity: input.requestIdentity,
              });
              const contested = await acquireRunDispatchLease({
                runId: claims[0].run.id,
                requestIdentity: input.requestIdentity,
              });
              const dispatch = globalThis.__wajeConversationMemoryStore
                .runDispatches.get(claims[0].run.id);
              dispatch.leaseExpiresAt = "2000-01-01T00:00:00.000Z";
              const replacement = await acquireRunDispatchLease({
                runId: claims[0].run.id,
                requestIdentity: input.requestIdentity,
              });
              const stale = await failOwnedRunDispatch({
                runId: claims[0].run.id,
                ownerId: first.ownerId,
                leaseEpoch: first.leaseEpoch,
                failureReason: "stale-owner",
              });
              const current = await failOwnedRunDispatch({
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


def test_real_message_and_artifact_routes_share_stable_request_outbox():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const fs = require("fs");
            const path = require("path");
            const out = process.env.GATEWAY_OUT;
            const thread = {
              id: "thread-producers",
              ownerId: "local-user",
              topicIds: ["topic-producers"],
              messages: [],
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            const artifact = {
              id: "artifact-producers",
              threadId: thread.id,
              topicId: "topic-producers",
              snapshotId: "snapshot-producers",
              followUpContext: "continue",
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map(),
              artifacts: new Map([[artifact.id, artifact]]),
              memoryProposals: new Map(),
              clarificationResolutions: new Map(),
              clarificationExecutionAttempts: new Map(),
              runDispatches: new Map(),
              auditEvents: [],
            };
            const messages = require(
              out + "/app/api/threads/[threadId]/messages/route.js"
            );
            const continued = require(
              out + "/app/api/artifacts/[artifactId]/continue/route.js"
            );
            const messageContext = {
              params: Promise.resolve({ threadId: thread.id }),
            };
            const artifactContext = {
              params: Promise.resolve({ artifactId: artifact.id }),
            };
            const request = (url, requestIdentity, body) => new NextRequest(url, {
              method: "POST",
              headers: {
                "content-type": "application/json",
                ...(requestIdentity ? { "idempotency-key": requestIdentity } : {}),
              },
              body: JSON.stringify(body),
            });
            (async () => {
              process.env.WAJE_AGENT_CORE_INLINE = "1";
              const missingIdentity = await messages.POST(
                request(
                  "http://localhost/api/threads/thread-producers/messages",
                  "",
                  { message: "缺少稳定请求标识" },
                ),
                messageContext,
              );
              const messageResponses = await Promise.all([
                messages.POST(request(
                  "http://localhost/api/threads/thread-producers/messages",
                  "message-request-1",
                  { message: "检查昨天付费金额" },
                ), messageContext),
                messages.POST(request(
                  "http://localhost/api/threads/thread-producers/messages",
                  "message-request-1",
                  { message: "检查昨天付费金额" },
                ), messageContext),
              ]);
              const messageBodies = await Promise.all(
                messageResponses.map((response) => response.json())
              );
              const messageConflict = await messages.POST(request(
                "http://localhost/api/threads/thread-producers/messages",
                "message-request-1",
                { message: "检查今天付费金额" },
              ), messageContext);
              const artifactResponses = await Promise.all([
                continued.POST(request(
                  "http://localhost/api/artifacts/artifact-producers/continue",
                  "artifact-request-1",
                  { message: "继续检查渠道", threadId: thread.id },
                ), artifactContext),
                continued.POST(request(
                  "http://localhost/api/artifacts/artifact-producers/continue",
                  "artifact-request-1",
                  { message: "继续检查渠道", threadId: thread.id },
                ), artifactContext),
              ]);
              const artifactBodies = await Promise.all(
                artifactResponses.map((response) => response.json())
              );
              const artifactConflict = await continued.POST(request(
                "http://localhost/api/artifacts/artifact-producers/continue",
                "artifact-request-1",
                { message: "改查地区", threadId: thread.id },
              ), artifactContext);
              const store = globalThis.__wajeConversationMemoryStore;
              const counterPath = path.join(
                process.env.WAJE_GATEWAY_TEST_TMP,
                "agent-invocations",
              );
              console.log(JSON.stringify({
                missingIdentity: missingIdentity.status,
                messageStatuses: messageResponses.map((item) => item.status),
                messageRunIds: messageBodies.map((item) => item.run.id),
                messageConflict: messageConflict.status,
                artifactStatuses: artifactResponses.map((item) => item.status),
                artifactRunIds: artifactBodies.map((item) => item.run.id),
                artifactConflict: artifactConflict.status,
                producerKinds: [...store.runDispatches.values()]
                  .map((item) => item.producerKind).sort(),
                messageCount: thread.messages.length,
                runCount: store.runs.size,
                dispatchCount: store.runDispatches.size,
                invocationCount: fs.existsSync(counterPath)
                  ? fs.readFileSync(counterPath, "utf8").trim().split("\\n").length
                  : 0,
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        fake_python=_successful_agent_core(),
    )

    assert result["missingIdentity"] == 400
    assert result["messageStatuses"] == [202, 202]
    assert len(set(result["messageRunIds"])) == 1
    assert result["messageConflict"] == 409
    assert result["artifactStatuses"] == [202, 202]
    assert len(set(result["artifactRunIds"])) == 1
    assert result["artifactConflict"] == 409
    assert result["producerKinds"] == ["artifact_continue", "thread_message"]
    assert result["messageCount"] == 2
    assert result["runCount"] == 2
    assert result["dispatchCount"] == 2
    assert result["invocationCount"] == 2


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
            key in explicit_env
            for key in ("WAJE_RUNTIME_DATABASE_URL", "DATABASE_URL")
        ):
            process_env.pop("WAJE_RUNTIME_DATABASE_URL", None)
            process_env.pop("DATABASE_URL", None)
        if fake_python is not None:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            executable = bin_dir / "python3"
            executable.write_text(
                fake_python.replace(
                    "#!/usr/bin/env python3",
                    f"#!{sys.executable}",
                    1,
                ),
                encoding="utf-8",
            )
            executable.chmod(
                executable.stat().st_mode
                | stat.S_IXUSR
                | stat.S_IXGRP
                | stat.S_IXOTH
            )
            process_env["PATH"] = f"{bin_dir}:{process_env['PATH']}"
        process_env.update(explicit_env)
        result = subprocess.run(
            ["node", "-e", source],
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
            "answer_package": {"run_id": run_id, "status": "completed"},
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
              const deadline = Date.now() + 2000;
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
              artifacts: new Map(),
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
              const deadline = Date.now() + 2000;
              while (
                store.runs.get(body.run.id)?.status !== "failed"
                && Date.now() < deadline
              ) {
                await new Promise((resolve) => setTimeout(resolve, 20));
              }
              const run = store.runs.get(body.run.id);
              const dispatch = store.runDispatches.get(body.run.id);
              console.log(JSON.stringify({
                httpStatus: response.status,
                initialAgentStatus: body.agentCore.status,
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
    assert result["initialAgentStatus"] == "started"
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
              artifacts: new Map(),
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
        env={"WAJE_AGENT_CORE_COMMAND": "unavailable-command"},
    )

    assert result["status"] == 503
    assert result["body"]["error"] == "agent_core_spawn_failed"
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
