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
    "app/api/_generalAgent.ts",
    "app/api/_pythonRuntime.ts",
    "app/api/_conversationStore.ts",
)


def test_message_route_starts_general_agent_without_precreating_bi_run() -> None:
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const fs = require("fs");
            const path = require("path");
            const out = process.env.GATEWAY_OUT;
            const thread = {
              id: "thread-general-entry",
              ownerId: "local-user",
              topicIds: [],
              messages: [],
              createdAt: "2026-07-21T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map(),
              memoryProposals: new Map(),
              runDispatches: new Map(),
              auditEvents: [],
            };
            const { POST } = require(
              out + "/app/api/threads/[threadId]/messages/route.js"
            );
            (async () => {
              const response = await POST(
                new NextRequest(
                  "http://localhost/api/threads/thread-general-entry/messages",
                  {
                    method: "POST",
                    headers: {
                      "content-type": "application/json",
                      "idempotency-key": "request-general-1",
                    },
                    body: JSON.stringify({
                      message: "解释你可以完成哪些分析任务。",
                      requestIdentity: "request-general-1",
                    }),
                  },
                ),
                { params: Promise.resolve({ threadId: thread.id }) },
              );
              const commandPath = path.join(
                process.env.WAJE_GATEWAY_TEST_TMP,
                "general-agent-command.json",
              );
              const deadline = Date.now() + 2000;
              while (!fs.existsSync(commandPath) && Date.now() < deadline) {
                await new Promise((resolve) => setTimeout(resolve, 20));
              }
              const store = globalThis.__wajeConversationMemoryStore;
              console.log(JSON.stringify({
                status: response.status,
                body: await response.json(),
                command: JSON.parse(fs.readFileSync(commandPath, "utf8")),
                runCount: store.runs.size,
                dispatchCount: store.runDispatches.size,
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        fake_python=_general_agent_ack_script(),
    )

    assert result["status"] == 202
    assert result["command"] == {
        "threadId": "thread-general-entry",
        "actorId": "local-user",
        "operationId": "request-general-1",
        "message": "解释你可以完成哪些分析任务。",
    }
    assert result["runCount"] == 0
    assert result["dispatchCount"] == 0
    assert result["body"]["snapshot"]["transport"]["runHandle"] is None
    assert result["body"]["snapshot"]["transport"]["eventsUrl"] == (
        "/api/threads/thread-general-entry/events"
    )


def test_message_route_forwards_only_typed_pending_action_resolution() -> None:
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const fs = require("fs");
            const path = require("path");
            const out = process.env.GATEWAY_OUT;
            const thread = {
              id: "thread-agent-action",
              ownerId: "local-user",
              topicIds: [],
              messages: [],
              createdAt: "2026-07-21T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map(), memoryProposals: new Map(),
              runDispatches: new Map(), auditEvents: [],
            };
            const { POST } = require(
              out + "/app/api/threads/[threadId]/messages/route.js"
            );
            const send = (identity, resolution) => POST(
              new NextRequest(
                "http://localhost/api/threads/thread-agent-action/messages",
                {
                  method: "POST",
                  headers: {
                    "content-type": "application/json",
                    "idempotency-key": identity,
                  },
                  body: JSON.stringify({
                    message: "采用推荐口径。",
                    requestIdentity: identity,
                    pendingActionResolution: resolution,
                  }),
                },
              ),
              { params: Promise.resolve({ threadId: thread.id }) },
            );
            (async () => {
              const valid = await send("request-action-valid", {
                actionRef: "pending-action:1",
                decision: "answered",
                selectedOptionId: "recommended",
                answerText: "采用推荐口径。",
              });
              const invalid = await send("request-action-invalid", {
                actionRef: "pending-action:1",
                decision: "answered",
                selectedOptionId: "recommended",
                answerText: "不同文本",
              });
              const commandPath = path.join(
                process.env.WAJE_GATEWAY_TEST_TMP,
                "general-agent-command.json",
              );
              const deadline = Date.now() + 2000;
              while (!fs.existsSync(commandPath) && Date.now() < deadline) {
                await new Promise((resolve) => setTimeout(resolve, 20));
              }
              console.log(JSON.stringify({
                validStatus: valid.status,
                invalidStatus: invalid.status,
                invalidBody: await invalid.json(),
                command: JSON.parse(fs.readFileSync(commandPath, "utf8")),
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        fake_python=_general_agent_ack_script(),
    )

    assert result["validStatus"] == 202
    assert result["invalidStatus"] == 400
    assert result["invalidBody"]["error"]["code"] == "request_invalid"
    assert result["command"]["pendingActionResolution"] == {
        "actionRef": "pending-action:1",
        "decision": "answered",
        "selectedOptionId": "recommended",
        "answerText": "采用推荐口径。",
    }


def test_general_agent_requires_authoritative_startup_acknowledgment() -> None:
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            const { runGeneralAgentTurn } = require(out + "/app/api/_generalAgent.js");
            (async () => {
              const result = await runGeneralAgentTurn({
                threadId: "thread-startup",
                actorId: "local-user",
                operationId: "request-startup",
                message: "检查付费金额",
              });
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
    assert result["error"] == "general_agent_startup_failed"


def test_general_agent_inline_result_is_sdk_neutral() -> None:
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            const { runGeneralAgentTurn } = require(out + "/app/api/_generalAgent.js");
            (async () => {
              const result = await runGeneralAgentTurn({
                threadId: "thread-inline",
                actorId: "local-user",
                operationId: "request-inline",
                message: "你好",
              }, { forceInline: true });
              console.log(JSON.stringify(result));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        fake_python=textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            print(json.dumps({
                "status": "completed",
                "message": {"role": "assistant", "text": "你好", "createdAt": "2026-07-21T00:00:00Z"},
                "transport": {"stateVersion": "2", "latestItemSequence": 3},
            }, ensure_ascii=False))
            """
        ),
    )

    assert result["status"] == "completed"
    assert result["result"]["message"]["text"] == "你好"
    assert "RunResult" not in json.dumps(result)


def test_message_route_startup_failure_does_not_create_bi_authority() -> None:
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const out = process.env.GATEWAY_OUT;
            const thread = {
              id: "thread-startup-failure", ownerId: "local-user",
              topicIds: [], messages: [], createdAt: "2026-07-21T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map(), memoryProposals: new Map(),
              runDispatches: new Map(), auditEvents: [],
            };
            const { POST } = require(
              out + "/app/api/threads/[threadId]/messages/route.js"
            );
            (async () => {
              const response = await POST(
                new NextRequest(
                  "http://localhost/api/threads/thread-startup-failure/messages",
                  {
                    method: "POST",
                    headers: {
                      "content-type": "application/json",
                      "idempotency-key": "request-startup-failure",
                    },
                    body: JSON.stringify({ message: "检查付费金额" }),
                  },
                ),
                { params: Promise.resolve({ threadId: thread.id }) },
              );
              console.log(JSON.stringify({
                status: response.status,
                body: await response.json(),
                runCount: globalThis.__wajeConversationMemoryStore.runs.size,
                dispatchCount: globalThis.__wajeConversationMemoryStore.runDispatches.size,
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        env={"PATH": "/nonexistent"},
    )

    assert result["status"] == 503
    assert result["body"]["error"]["code"] == "analysis_unavailable"
    assert result["runCount"] == 0
    assert result["dispatchCount"] == 0


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
                "npx", "tsc", "--ignoreConfig", "--noEmit", "false",
                "--module", "node16", "--moduleResolution", "node16",
                "--target", "ES2022", "--esModuleInterop", "--skipLibCheck",
                "--outDir", str(out_dir), "--rootDir", str(ROOT),
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
        process_env.pop("WAJE_RUNTIME_DATABASE_URL", None)
        process_env.pop("DATABASE_URL", None)
        if fake_python is not None:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            executable = bin_dir / "uv"
            executable.write_text(
                fake_python.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1),
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


def _general_agent_ack_script() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import json
        import os
        import sys
        import time

        command = json.loads(sys.argv[sys.argv.index("--command-json") + 1])
        target = os.path.join(
            os.environ["WAJE_GATEWAY_TEST_TMP"],
            "general-agent-command.json",
        )
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(command, handle, ensure_ascii=False)
        os.write(
            int(os.environ["WAJE_GENERAL_AGENT_STARTUP_ACK_FD"]),
            b"WAJE_GENERAL_AGENT_RUNNING\\n",
        )
        time.sleep(0.05)
        """
    )
