from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[2]


def _run_store(source: str) -> dict:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node executable is required")
    env = {
        **os.environ,
        "NODE_ENV": "test",
        "WAJE_GATEWAY_UNIT_TEST_STORE": "memory",
    }
    env.pop("WAJE_RUNTIME_DATABASE_URL", None)
    env.pop("DATABASE_URL", None)
    completed = subprocess.run(
        [
            node,
            "--no-warnings",
            "--experimental-loader=./tests/support/typescript-extension-loader.mjs",
            "--experimental-strip-types",
            "--input-type=module",
            "-e",
            textwrap.dedent(source),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_initial_thread_claim_is_idempotent_for_one_customer_operation() -> None:
    result = _run_store(
        """
        globalThis.__wajeConversationMemoryStore = {
          threads: new Map(), runs: new Map(), memoryProposals: new Map(),
          runDispatches: new Map(), auditEvents: [],
        };
        const { claimInitialThreadRequest } = await import(
          "./app/api/_conversationStore.ts"
        );
        const first = await claimInitialThreadRequest("customer-1", "operation-1");
        const replay = await claimInitialThreadRequest("customer-1", "operation-1");
        const other = await claimInitialThreadRequest("customer-1", "operation-2");
        const store = globalThis.__wajeConversationMemoryStore;
        console.log(JSON.stringify({
          replayedSameThread: first.id === replay.id,
          otherThread: first.id !== other.id,
          threadCount: store.threads.size,
          createdAuditCount: store.auditEvents.filter(
            (event) => event.eventType === "thread_created"
          ).length,
        }));
        """
    )
    assert result == {
        "replayedSameThread": True,
        "otherThread": True,
        "threadCount": 2,
        "createdAuditCount": 2,
    }


def test_projection_contract_failure_is_audited_and_customer_safe() -> None:
    result = _run_store(
        """
        globalThis.__wajeConversationMemoryStore = {
          threads: new Map(), runs: new Map(), memoryProposals: new Map(),
          runDispatches: new Map(), auditEvents: [],
        };
        const { customerJsonError } = await import(
          "./app/api/_conversationStore.ts"
        );
        const response = await customerJsonError(
          new Error("customer_publication_invalid"),
          { threadId: "thread-internal" },
        );
        const body = await response.json();
        const audit = globalThis.__wajeConversationMemoryStore.auditEvents.at(-1);
        console.log(JSON.stringify({
          status: response.status,
          publicCode: body.error.code,
          publicMessage: body.error.message,
          hasTechnicalDetailRef: Boolean(body.transport.technicalDetailRef),
          auditedInternalCode: audit.payload.internalCode,
        }));
        """
    )
    assert result["status"] == 500
    assert result["publicCode"] == "analysis_unavailable"
    assert "customer_publication_invalid" not in result["publicMessage"]
    assert result["hasTechnicalDetailRef"] is True
    assert result["auditedInternalCode"] == "customer_publication_invalid"
