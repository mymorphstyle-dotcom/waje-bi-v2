from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.conversation.postgres_store import PostgresConversationStore
from tests.phase7.test_terminal_run_status import _RunStatusConnection


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


def test_artifact_handlers_preserve_typed_permission_and_thread_boundaries():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const out = process.env.GATEWAY_OUT;
            const thread = {
              id: "thread-artifact",
              ownerId: "owner-1",
              topicIds: ["topic-artifact"],
              messages: [],
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            const artifact = {
              id: "artifact-reviewed",
              threadId: thread.id,
              topicId: "topic-artifact",
              snapshotId: "snapshot-reviewed",
              permissionScope: "analyst",
              followUpContext: "continue",
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map(),
              artifacts: new Map([[artifact.id, artifact]]),
              memoryProposals: new Map(),
              clarificationResumeClaims: new Map(),
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
              process.env.WAJE_GATEWAY_ROLE = "business_reader";
              const missing = await open.GET(
                new Request("http://localhost/api/artifacts/missing"),
                context("missing"),
              );
              const deniedOpen = await open.GET(
                new Request("http://localhost/api/artifacts/artifact-reviewed"),
                context(artifact.id),
              );
              const deniedExport = await exported.GET(
                new Request("http://localhost/api/artifacts/artifact-reviewed/export"),
                context(artifact.id),
              );
              const deniedContinue = await continued.POST(
                new NextRequest(
                  "http://localhost/api/artifacts/artifact-reviewed/continue",
                  {
                    method: "POST",
                    headers: { "content-type": "application/json" },
                    body: JSON.stringify({ message: "继续" }),
                  },
                ),
                context(artifact.id),
              );
              process.env.WAJE_GATEWAY_ROLE = "analyst";
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
                  deniedOpen.status,
                  deniedExport.status,
                  deniedContinue.status,
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

    assert result["statuses"] == [404, 403, 403, 403, 409, 200]
    assert result["messageCount"] == 0
    assert result["runCount"] == 0
    assert result["auditTypes"].count("artifact_continue_blocked") >= 3
    assert "artifact_opened" in result["auditTypes"]


def test_shared_run_dispatch_outbox_replays_conflicts_and_fences_stale_owner():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            const thread = {
              id: "thread-shared-dispatch",
              ownerId: "owner-1",
              topicIds: [],
              messages: [],
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map(),
              artifacts: new Map(),
              memoryProposals: new Map(),
              clarificationResumeClaims: new Map(),
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
              ownerId: "owner-1",
              topicIds: ["topic-producers"],
              messages: [],
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            const artifact = {
              id: "artifact-producers",
              threadId: thread.id,
              topicId: "topic-producers",
              snapshotId: "snapshot-producers",
              permissionScope: "analyst",
              followUpContext: "continue",
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map(),
              artifacts: new Map([[artifact.id, artifact]]),
              memoryProposals: new Map(),
              clarificationResumeClaims: new Map(),
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
              process.env.WAJE_GATEWAY_ROLE = "analyst";
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
                "commonjs",
                "--moduleResolution",
                "node",
                "--target",
                "ES2022",
                "--esModuleInterop",
                "--skipLibCheck",
                "--ignoreDeprecations",
                "6.0",
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

        process_env = {
            **os.environ,
            "NODE_PATH": str(ROOT / "node_modules"),
            "GATEWAY_OUT": str(out_dir),
            "WAJE_GATEWAY_TEST_TMP": tmp,
            "NODE_ENV": "test",
        }
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
        process_env.update(env or {})
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


def test_real_clarification_route_claims_identical_replay_once_and_conflicts_closed():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const fs = require("fs");
            const path = require("path");
            const out = process.env.GATEWAY_OUT;
            const source = {
              id: "run-source",
              threadId: "thread-source",
              status: "waiting_for_clarification",
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            const thread = {
              id: "thread-source",
              ownerId: "owner-1",
              topicIds: ["topic-source"],
              messages: [],
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map([[source.id, source]]),
              artifacts: new Map(),
              memoryProposals: new Map(),
              clarificationResumeClaims: new Map(),
              auditEvents: [],
            };
            const { POST } = require(
              out + "/app/api/runs/[runId]/clarifications/route.js"
            );
            const request = (selectedOptionId, requestIdentity = "clarification-stable-1") => {
              const headers = { "content-type": "application/json" };
              if (requestIdentity) headers["idempotency-key"] = requestIdentity;
              return new NextRequest(
                "http://localhost/api/runs/run-source/clarifications",
                {
                  method: "POST",
                  headers,
                  body: JSON.stringify({
                    answer: "  按推荐继续  ",
                    selectedOptionId,
                  }),
                },
              );
            };
            const context = { params: Promise.resolve({ runId: "run-source" }) };
            (async () => {
              const missing = await POST(request("recommended", ""), context);
              const missingBody = await missing.json();
              const identical = await Promise.all([
                POST(request("recommended"), context),
                POST(request("recommended"), context),
              ]);
              const identicalBodies = await Promise.all(identical.map((r) => r.json()));
              const conflict = await POST(request("different-choice"), context);
              const conflictBody = await conflict.json();
              const store = globalThis.__wajeConversationMemoryStore;
              const resumedRuns = [...store.runs.values()].filter(
                (run) => run.id !== "run-source"
              );
              const counterPath = path.join(
                process.env.WAJE_GATEWAY_TEST_TMP,
                "agent-invocations",
              );
              console.log(JSON.stringify({
                missingStatus: missing.status,
                missingBody,
                identicalStatuses: identical.map((r) => r.status),
                resumedIds: identicalBodies.map((body) => body.resumedRunId),
                conflictStatus: conflict.status,
                conflictBody,
                source: store.runs.get("run-source"),
                resumedRuns,
                messageCount: store.threads.get("thread-source").messages.length,
                claimCount: store.clarificationResumeClaims?.size ?? 0,
                invocationCount: fs.existsSync(counterPath)
                  ? fs.readFileSync(counterPath, "utf8").trim().split("\\n").length
                  : 0,
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        fake_python=_successful_agent_core(),
    )

    assert result["missingStatus"] == 400, result
    assert result["missingBody"]["error"] == "run_dispatch_request_identity_required"
    assert result["identicalStatuses"] == [200, 200], result
    assert len(set(result["resumedIds"])) == 1
    assert result["conflictStatus"] == 409
    assert result["conflictBody"]["error"] == "run_dispatch_conflict"
    assert result["source"]["status"] == "waiting_for_clarification"
    assert len(result["resumedRuns"]) == 1
    assert result["messageCount"] == 1
    assert result["claimCount"] == 1
    assert result["invocationCount"] == 1


def test_committed_clarification_claim_is_recovered_once_by_concurrent_retries():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const fs = require("fs");
            const path = require("path");
            const out = process.env.GATEWAY_OUT;
            const source = {
              id: "run-source-recovery",
              threadId: "thread-recovery",
              status: "waiting_for_clarification",
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            const thread = {
              id: "thread-recovery",
              ownerId: "owner-1",
              topicIds: [],
              messages: [],
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map([[source.id, source]]),
              artifacts: new Map(),
              memoryProposals: new Map(),
              clarificationResumeClaims: new Map(),
              auditEvents: [],
            };
            const { claimClarificationResume } = require(
              out + "/app/api/_conversationStore.js"
            );
            const { POST } = require(
              out + "/app/api/runs/[runId]/clarifications/route.js"
            );
            const request = () => new NextRequest(
              "http://localhost/api/runs/run-source-recovery/clarifications",
              {
                method: "POST",
                headers: {
                  "content-type": "application/json",
                  "idempotency-key": "clarification-recovery-1",
                },
                body: JSON.stringify({
                  answer: "按推荐继续",
                  selectedOptionId: "recommended",
                }),
              },
            );
            const context = {
              params: Promise.resolve({ runId: "run-source-recovery" }),
            };
            (async () => {
              const committed = await claimClarificationResume({
                sourceRunId: source.id,
                requestIdentity: "clarification-recovery-1",
                answer: "按推荐继续",
                selectedOptionId: "recommended",
                source: "user",
                runtimePermissionScope: "viewer",
              });
              const responses = await Promise.all([
                POST(request(), context),
                POST(request(), context),
              ]);
              const bodies = await Promise.all(responses.map((response) => response.json()));
              const counterPath = path.join(
                process.env.WAJE_GATEWAY_TEST_TMP,
                "agent-invocations",
              );
              console.log(JSON.stringify({
                committed,
                statuses: responses.map((response) => response.status),
                bodies,
                invocationCount: fs.existsSync(counterPath)
                  ? fs.readFileSync(counterPath, "utf8").trim().split("\\n").length
                  : 0,
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        fake_python=_successful_agent_core(),
    )

    assert result["committed"]["replayed"] is False
    assert result["statuses"] == [200, 200]
    assert {body["resumedRunId"] for body in result["bodies"]} == {
        result["committed"]["resumedRunId"]
    }
    assert result["invocationCount"] == 1
    assert {body["agentCore"]["status"] for body in result["bodies"]} == {
        "completed",
        "dispatch_in_progress",
    }


def test_expired_clarification_dispatch_owner_cannot_fail_new_owner_queue():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            const source = {
              id: "run-source-owner",
              threadId: "thread-owner",
              status: "waiting_for_clarification",
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            const thread = {
              id: "thread-owner",
              ownerId: "owner-1",
              topicIds: [],
              messages: [],
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map([[source.id, source]]),
              artifacts: new Map(),
              memoryProposals: new Map(),
              clarificationResumeClaims: new Map(),
              auditEvents: [],
            };
            const {
              acquireClarificationResumeDispatch,
              claimClarificationResume,
              failClarificationResumeDispatch,
            } = require(out + "/app/api/_conversationStore.js");
            (async () => {
              const claim = await claimClarificationResume({
                sourceRunId: source.id,
                requestIdentity: "clarification-expiry-1",
                answer: "按推荐继续",
                selectedOptionId: "recommended",
                source: "user",
                runtimePermissionScope: "viewer",
              });
              const first = await acquireClarificationResumeDispatch({
                sourceRunId: source.id,
                resumedRunId: claim.resumedRunId,
                requestIdentity: claim.requestIdentity,
              });
              const storedClaim = globalThis.__wajeConversationMemoryStore
                .clarificationResumeClaims.get(source.id);
              storedClaim.dispatchLeaseExpiresAt = "2000-01-01T00:00:00.000Z";
              const replacement = await acquireClarificationResumeDispatch({
                sourceRunId: source.id,
                resumedRunId: claim.resumedRunId,
                requestIdentity: claim.requestIdentity,
              });
              const staleFailure = await failClarificationResumeDispatch({
                sourceRunId: source.id,
                resumedRunId: claim.resumedRunId,
                ownerId: first.ownerId,
                failureReason: "agent_core_process_failed",
              });
              const currentFailure = await failClarificationResumeDispatch({
                sourceRunId: source.id,
                resumedRunId: claim.resumedRunId,
                ownerId: replacement.ownerId,
                failureReason: "agent_core_startup_failed",
              });
              console.log(JSON.stringify({
                first,
                replacement,
                staleFailure,
                currentFailure,
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
    )

    assert result["first"]["acquired"] is True
    assert result["replacement"]["acquired"] is True
    assert result["first"]["ownerId"] != result["replacement"]["ownerId"]
    assert result["staleFailure"]["status"] == "queued"
    assert result["currentFailure"]["status"] == "failed"
    assert result["currentFailure"]["request"]["failure_reason"] == (
        "agent_core_startup_failed"
    )


def test_clarification_submission_shape_is_rejected_before_one_shot_claim():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const fs = require("fs");
            const path = require("path");
            const out = process.env.GATEWAY_OUT;
            const source = {
              id: "run-source-boundary",
              threadId: "thread-boundary",
              status: "waiting_for_clarification",
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            const thread = {
              id: "thread-boundary",
              ownerId: "owner-1",
              topicIds: [],
              messages: [],
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map([[source.id, source]]),
              artifacts: new Map(),
              memoryProposals: new Map(),
              clarificationResumeClaims: new Map(),
              auditEvents: [],
            };
            const { POST } = require(
              out + "/app/api/runs/[runId]/clarifications/route.js"
            );
            const request = (body) => new NextRequest(
              "http://localhost/api/runs/run-source-boundary/clarifications",
              {
                method: "POST",
                headers: {
                  "content-type": "application/json",
                  "idempotency-key": "clarification-boundary-1",
                },
                body: JSON.stringify(body),
              },
            );
            const context = {
              params: Promise.resolve({ runId: "run-source-boundary" }),
            };
            (async () => {
              const dirtyAnswer = await POST(request({
                answer: { text: "按推荐继续" },
                selectedOptionId: "recommended",
              }), context);
              const dirtyAnswerBody = await dirtyAnswer.json();
              const dirtyOption = await POST(request({
                answer: "按推荐继续",
                selectedOptionId: { id: "recommended" },
              }), context);
              const dirtyOptionBody = await dirtyOption.json();
              const storeAfterDirty = globalThis.__wajeConversationMemoryStore;
              const dirtyClaimCount = storeAfterDirty.clarificationResumeClaims.size;
              const dirtyRunCount = storeAfterDirty.runs.size;
              const valid = await POST(request({
                answer: "按推荐继续",
                selectedOptionId: "recommended",
              }), context);
              const validBody = await valid.json();
              const counterPath = path.join(
                process.env.WAJE_GATEWAY_TEST_TMP,
                "agent-invocations",
              );
              console.log(JSON.stringify({
                dirtyAnswerStatus: dirtyAnswer.status,
                dirtyAnswerBody,
                dirtyOptionStatus: dirtyOption.status,
                dirtyOptionBody,
                dirtyClaimCount,
                dirtyRunCount,
                validStatus: valid.status,
                validBody,
                finalClaimCount: storeAfterDirty.clarificationResumeClaims.size,
                invocationCount: fs.existsSync(counterPath)
                  ? fs.readFileSync(counterPath, "utf8").trim().split("\\n").length
                  : 0,
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        fake_python=_successful_agent_core(),
    )

    assert result["dirtyAnswerStatus"] == 400
    assert result["dirtyAnswerBody"]["error"] == "clarification_answer_required"
    assert result["dirtyOptionStatus"] == 400
    assert result["dirtyOptionBody"]["error"] == "clarification_selected_option_invalid"
    assert result["dirtyClaimCount"] == 0
    assert result["dirtyRunCount"] == 1
    assert result["validStatus"] == 200
    assert result["validBody"]["agentCore"]["status"] == "completed"
    assert result["finalClaimCount"] == 1
    assert result["invocationCount"] == 1


def test_real_clarification_route_maps_lifecycle_and_agent_core_failures():
    fake_python = textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import json
        import sys

        run_id = sys.argv[sys.argv.index("--run-id") + 1]
        message = sys.argv[sys.argv.index("--message") + 1]
        if message == "malformed":
            print("not-json")
        elif message == "mismatch":
            print(json.dumps({
                "status": "completed",
                "run_id": "run-other",
                "turn_id": "turn-other",
                "topic_id": "topic-other",
                "context_manifest": {},
                "answer_package": {"run_id": "run-other"},
            }))
        else:
            print(json.dumps({
                "status": "completed",
                "run_id": run_id,
                "turn_id": "turn-ok",
                "topic_id": "topic-ok",
                "context_manifest": {},
                "answer_package": {"run_id": run_id},
            }))
        """
    )
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const out = process.env.GATEWAY_OUT;
            const threads = new Map();
            const runs = new Map();
            for (const [suffix, status] of [
              ["conflict", "queued"],
              ["malformed", "waiting_for_clarification"],
              ["mismatch", "waiting_for_clarification"],
            ]) {
              const threadId = "thread-" + suffix;
              threads.set(threadId, {
                id: threadId,
                ownerId: "owner-1",
                topicIds: [],
                messages: [],
                createdAt: "2026-07-14T00:00:00.000Z",
              });
              runs.set("run-" + suffix, {
                id: "run-" + suffix,
                threadId,
                status,
                createdAt: "2026-07-14T00:00:00.000Z",
              });
            }
            globalThis.__wajeConversationMemoryStore = {
              threads,
              runs,
              artifacts: new Map(),
              memoryProposals: new Map(),
              clarificationResumeClaims: new Map(),
              auditEvents: [],
            };
            const { POST } = require(
              out + "/app/api/runs/[runId]/clarifications/route.js"
            );
            const post = async (runId, answer) => {
              const response = await POST(
                new NextRequest("http://localhost/api/runs/" + runId + "/clarifications", {
                  method: "POST",
                  headers: {
                    "content-type": "application/json",
                    "idempotency-key": "clarification-" + runId,
                  },
                  body: JSON.stringify({ answer }),
                }),
                { params: Promise.resolve({ runId }) },
              );
              return { status: response.status, body: await response.json() };
            };
            (async () => {
              const missing = await post("run-missing", "answer");
              const conflict = await post("run-conflict", "answer");
              const malformed = await post("run-malformed", "malformed");
              const mismatch = await post("run-mismatch", "mismatch");
              const store = globalThis.__wajeConversationMemoryStore;
              const resumed = [...store.runs.values()].filter(
                (run) => run.id !== "run-conflict"
                  && run.id !== "run-malformed"
                  && run.id !== "run-mismatch"
              );
              console.log(JSON.stringify({ missing, conflict, malformed, mismatch, resumed }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        fake_python=fake_python,
    )

    assert result["missing"]["status"] == 404
    assert result["conflict"]["status"] == 409
    assert result["malformed"]["status"] == 502
    assert result["mismatch"]["status"] == 502
    failed = [run for run in result["resumed"] if run["status"] == "failed"]
    assert len(failed) == 2
    assert {
        run.get("request", {}).get("failure_reason") for run in failed
    } == {
        "agent_core_output_malformed_json",
        "agent_core_run_id_mismatch",
    }


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
              ownerId: "owner-1",
              topicIds: [],
              messages: [],
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map(),
              artifacts: new Map(),
              memoryProposals: new Map(),
              clarificationResumeClaims: new Map(),
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


def test_real_clarification_route_maps_startup_unavailable_to_503_and_terminalizes_queue():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const out = process.env.GATEWAY_OUT;
            const thread = {
              id: "thread-startup",
              ownerId: "owner-1",
              topicIds: [],
              messages: [],
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            const source = {
              id: "run-source-startup",
              threadId: thread.id,
              status: "waiting_for_clarification",
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map([[source.id, source]]),
              artifacts: new Map(),
              memoryProposals: new Map(),
              clarificationResumeClaims: new Map(),
              auditEvents: [],
            };
            const { POST } = require(
              out + "/app/api/runs/[runId]/clarifications/route.js"
            );
            (async () => {
              const response = await POST(
                new NextRequest(
                  "http://localhost/api/runs/run-source-startup/clarifications",
                  {
                    method: "POST",
                    headers: {
                      "content-type": "application/json",
                      "idempotency-key": "clarification-startup-1",
                    },
                    body: JSON.stringify({ answer: "按推荐继续" }),
                  },
                ),
                { params: Promise.resolve({ runId: "run-source-startup" }) },
              );
              const store = globalThis.__wajeConversationMemoryStore;
              const resumed = [...store.runs.values()].filter(
                (run) => run.id !== "run-source-startup"
              );
              console.log(JSON.stringify({
                status: response.status,
                body: await response.json(),
                resumed,
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        env={"WAJE_AGENT_CORE_COMMAND": "unavailable-command"},
    )

    assert result["status"] == 503
    assert result["body"]["error"] == "agent_core_spawn_failed"
    assert len(result["resumed"]) == 1
    assert result["resumed"][0]["status"] == "failed"
    assert result["resumed"][0]["request"]["failure_reason"] == "agent_core_spawn_failed"


def test_real_message_route_maps_known_startup_failure_to_503_and_terminalizes_queue():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const out = process.env.GATEWAY_OUT;
            const thread = {
              id: "thread-message-startup",
              ownerId: "owner-1",
              topicIds: [],
              messages: [],
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map(),
              artifacts: new Map(),
              memoryProposals: new Map(),
              clarificationResumeClaims: new Map(),
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


def test_queued_failure_cas_records_reason_and_audits_without_overwriting_running():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            const queued = {
              id: "run-queued",
              threadId: "thread-1",
              status: "queued",
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            const running = {
              id: "run-running",
              threadId: "thread-1",
              status: "running",
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            const source = {
              id: "run-source-replay",
              threadId: "thread-1",
              status: "waiting_for_clarification",
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            const thread = {
              id: "thread-1",
              ownerId: "owner-1",
              topicIds: [],
              messages: [],
              createdAt: "2026-07-14T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map([
                [queued.id, queued],
                [running.id, running],
                [source.id, source],
              ]),
              artifacts: new Map(),
              memoryProposals: new Map(),
              clarificationResumeClaims: new Map(),
              auditEvents: [],
            };
            const { claimClarificationResume, failQueuedRunDispatch } = require(
              out + "/app/api/_conversationStore.js"
            );
            (async () => {
              const failed = await failQueuedRunDispatch(
                "run-queued",
                "agent_core_startup_failed",
              );
              const preserved = await failQueuedRunDispatch(
                "run-running",
                "agent_core_output_malformed_json",
              );
              const claimInput = {
                sourceRunId: source.id,
                requestIdentity: "clarification-failed-queue-1",
                answer: "按推荐继续",
                selectedOptionId: "recommended",
                source: "user",
                runtimePermissionScope: "viewer",
              };
              const claim = await claimClarificationResume(claimInput);
              await failQueuedRunDispatch(
                claim.resumedRunId,
                "agent_core_startup_failed",
              );
              const replayedClaim = await claimClarificationResume(claimInput);
              const store = globalThis.__wajeConversationMemoryStore;
              console.log(JSON.stringify({
                failed,
                preserved,
                replayedClaim,
                audits: store.auditEvents,
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        )
    )

    assert result["failed"]["status"] == "failed"
    assert result["failed"]["request"]["failure_reason"] == "agent_core_startup_failed"
    assert result["preserved"]["status"] == "running"
    assert result["replayedClaim"]["run"]["status"] == "failed"
    assert result["replayedClaim"]["run"]["request"]["failure_reason"] == (
        "agent_core_startup_failed"
    )
    assert [event["eventType"] for event in result["audits"][:2]] == [
        "run_status_changed",
        "run_dispatch_failed",
    ]


def test_gateway_postgres_created_queue_is_claimed_by_real_python_store_contract():
    gateway = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            const state = { run: null, statements: [] };
            globalThis.__wajeConversationPool = {
              async query(statement, params = []) {
                state.statements.push(statement);
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
              const run = await createRun("thread-gateway-adapter");
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

    connection = _RunStatusConnection(
        status=gateway["state"]["run"]["status"],
        request=gateway["state"]["run"]["request"],
        thread_id=gateway["state"]["run"]["thread_id"],
        turn_id="",
        topic_id="",
    )
    store = PostgresConversationStore(connection)
    store.get_thread = lambda _thread_id: SimpleNamespace(owner_id="owner-gateway")
    with patch(
        "bi_agent.conversation.agent_core.ConversationRuntime.handle_message",
        side_effect=RuntimeError("conversation adapter stop"),
    ):
        with pytest.raises(RuntimeError, match="conversation adapter stop"):
            ConversationAgentCore(store).run_message(
                thread_id=gateway["run"]["threadId"],
                run_id=gateway["run"]["id"],
                user_message="检查昨天付费金额",
            )

    transitions = [
        params["status"]
        for statement, params in connection.statements
        if "analysis_run_status_transition_cas" in statement
    ]
    assert transitions == ["running", "failed"]
    assert connection.status == "failed"


def test_postgres_clarification_claim_transaction_replays_once_and_conflicts():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            const state = {
              source: {
                run_id: "run-source-pg",
                thread_id: "thread-source-pg",
                status: "waiting_for_clarification",
              },
              claim: null,
              dispatch: null,
              message: null,
              run: null,
              begins: 0,
              commits: 0,
              rollbacks: 0,
              sourceLocked: false,
              lockWaiters: [],
              inserts: { message: 0, run: 0, claim: 0, dispatch: 0, audit: 0 },
            };
            const makeClient = () => {
              let holdsSourceLock = false;
              const releaseSourceLock = () => {
                if (!holdsSourceLock) return;
                holdsSourceLock = false;
                const next = state.lockWaiters.shift();
                if (next) next();
                else state.sourceLocked = false;
              };
              return {
                async query(statement, params = []) {
                if (statement === "BEGIN") { state.begins += 1; return { rows: [] }; }
                if (statement === "COMMIT") {
                  state.commits += 1;
                  releaseSourceLock();
                  return { rows: [] };
                }
                if (statement === "ROLLBACK") {
                  state.rollbacks += 1;
                  releaseSourceLock();
                  return { rows: [] };
                }
                if (statement.includes("FROM waje_runtime.analysis_runs")
                    && statement.includes("FOR UPDATE")) {
                  if (state.sourceLocked) {
                    await new Promise((resolve) => state.lockWaiters.push(resolve));
                  } else {
                    state.sourceLocked = true;
                  }
                  holdsSourceLock = true;
                  return { rows: [state.source] };
                }
                if (statement.includes("FROM waje_runtime.clarification_resume_claims c")) {
                  if (!state.claim) return { rows: [] };
                  return { rows: [{
                    source_run_id: state.claim.source_run_id,
                    resumed_run_id: state.claim.resumed_run_id,
                    thread_id: state.claim.thread_id,
                    request_identity: state.claim.request_identity,
                    request_digest: state.dispatch.request_digest,
                    submission: state.claim.submission,
                    message_id: state.message.message_id,
                    role: "user",
                    text: state.message.text,
                    message_created_at: state.message.created_at,
                    status: state.run.status,
                    request: {},
                    run_created_at: state.run.created_at,
                  }] };
                }
                if (statement.includes("INSERT INTO waje_runtime.conversation_messages")) {
                  state.inserts.message += 1;
                  state.message = {
                    message_id: params[0], text: params[2],
                    created_at: "2026-07-14T00:00:00.000Z",
                  };
                  return { rows: [] };
                }
                if (statement.includes("INSERT INTO waje_runtime.analysis_runs")) {
                  state.inserts.run += 1;
                  state.run = {
                    run_id: params[0], status: "queued",
                    created_at: "2026-07-14T00:00:00.000Z",
                  };
                  return { rows: [] };
                }
                if (statement.includes("INSERT INTO waje_runtime.clarification_resume_claims")) {
                  state.inserts.claim += 1;
                  state.claim = {
                    source_run_id: params[0], resumed_run_id: params[1],
                    thread_id: params[2], request_identity: params[3],
                    submission: JSON.parse(params[4]), message_id: params[5],
                  };
                  return { rows: [] };
                }
                if (statement.includes("INSERT INTO waje_runtime.run_dispatches")) {
                  state.inserts.dispatch += 1;
                  state.dispatch = {
                    request_identity: params[2],
                    request_digest: params[3],
                    request_payload: JSON.parse(params[4]),
                  };
                  return { rows: [] };
                }
                if (statement.includes("INSERT INTO waje_runtime.audit_events")) {
                  state.inserts.audit += 1;
                  return { rows: [] };
                }
                throw new Error("unexpected_sql:" + statement);
                },
                release() {},
              };
            };
            globalThis.__wajeConversationPool = { connect: async () => makeClient() };
            const { claimClarificationResume } = require(
              out + "/app/api/_conversationStore.js"
            );
            (async () => {
              const input = {
                sourceRunId: "run-source-pg",
                requestIdentity: "clarification-pg-1",
                answer: "按推荐继续",
                selectedOptionId: "recommended",
                source: "user",
                runtimePermissionScope: "viewer",
              };
              const [first, replay] = await Promise.all([
                claimClarificationResume(input),
                claimClarificationResume(input),
              ]);
              let conflict = "";
              try {
                await claimClarificationResume({
                  ...input,
                  selectedOptionId: "different",
                });
              } catch (error) {
                conflict = error.code ?? error.message;
              }
              console.log(JSON.stringify({ first, replay, conflict, state }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        env={"WAJE_RUNTIME_DATABASE_URL": "postgres://transaction-adapter"},
    )

    assert result["first"]["resumedRunId"] == result["replay"]["resumedRunId"]
    assert result["first"]["replayed"] is False
    assert result["replay"]["replayed"] is True
    assert result["conflict"] == "run_dispatch_conflict"
    assert result["state"]["source"]["status"] == "waiting_for_clarification"
    assert result["state"]["inserts"] == {
        "message": 1,
        "run": 1,
        "claim": 1,
        "dispatch": 1,
        "audit": 1,
    }
    assert result["state"]["dispatch"]["request_payload"] == {
        "answer": "按推荐继续",
        "runId": "run-source-pg",
        "runtimePermissionScope": "viewer",
        "selectedOptionId": "recommended",
        "source": "user",
    }
    assert result["state"]["begins"] == 3
    assert result["state"]["commits"] == 2
    assert result["state"]["rollbacks"] == 1


def test_postgres_clarification_dispatch_lease_is_single_owner_under_concurrency():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            const state = {
              claim: {
                source_run_id: "run-source-lease",
                resumed_run_id: "run-resumed-lease",
                request_identity: "request-lease",
                dispatch_state: "pending",
                dispatch_owner_id: null,
                dispatch_lease_expires_at: null,
              },
              run: {
                run_id: "run-resumed-lease",
                thread_id: "thread-lease",
                status: "queued",
                request: {},
                created_at: "2026-07-14T00:00:00.000Z",
              },
              locked: false,
              waiters: [],
              begins: 0,
              commits: 0,
              rollbacks: 0,
              leaseWrites: 0,
            };
            const makeClient = () => {
              let holdsLock = false;
              const releaseLock = () => {
                if (!holdsLock) return;
                holdsLock = false;
                const next = state.waiters.shift();
                if (next) next();
                else state.locked = false;
              };
              return {
                async query(statement, params = []) {
                  if (statement === "BEGIN") { state.begins += 1; return { rows: [] }; }
                  if (statement === "COMMIT") {
                    state.commits += 1;
                    releaseLock();
                    return { rows: [] };
                  }
                  if (statement === "ROLLBACK") {
                    state.rollbacks += 1;
                    releaseLock();
                    return { rows: [] };
                  }
                  if (statement.includes("FROM waje_runtime.clarification_resume_claims c")
                      && statement.includes("FOR UPDATE OF c, r")) {
                    if (state.locked) {
                      await new Promise((resolve) => state.waiters.push(resolve));
                    } else {
                      state.locked = true;
                    }
                    holdsLock = true;
                    return { rows: [{
                      ...state.claim,
                      ...state.run,
                      dispatch_lease_active: state.claim.dispatch_state === "leased",
                    }] };
                  }
                  if (statement.includes("SET dispatch_state = 'leased'")) {
                    state.leaseWrites += 1;
                    state.claim.dispatch_state = "leased";
                    state.claim.dispatch_owner_id = params[1];
                    state.claim.dispatch_lease_expires_at = "2099-01-01T00:00:00.000Z";
                    return { rows: [] };
                  }
                  throw new Error("unexpected_sql:" + statement);
                },
                release() {},
              };
            };
            globalThis.__wajeConversationPool = { connect: async () => makeClient() };
            const { acquireClarificationResumeDispatch } = require(
              out + "/app/api/_conversationStore.js"
            );
            const input = {
              sourceRunId: state.claim.source_run_id,
              resumedRunId: state.claim.resumed_run_id,
              requestIdentity: state.claim.request_identity,
            };
            (async () => {
              const leases = await Promise.all([
                acquireClarificationResumeDispatch(input),
                acquireClarificationResumeDispatch(input),
              ]);
              console.log(JSON.stringify({ leases, state }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        env={"WAJE_RUNTIME_DATABASE_URL": "postgres://dispatch-adapter"},
    )

    acquired = [lease for lease in result["leases"] if lease["acquired"]]
    active = [
        lease
        for lease in result["leases"]
        if not lease["acquired"] and lease["reason"] == "active_lease"
    ]
    assert len(acquired) == 1
    assert len(active) == 1
    assert result["state"]["leaseWrites"] == 1
    assert result["state"]["begins"] == 2
    assert result["state"]["commits"] == 2
    assert result["state"]["rollbacks"] == 0
