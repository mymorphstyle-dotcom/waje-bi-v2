from __future__ import annotations

import textwrap
from pathlib import Path

from bi_agent.runtime.evidence_authority import canonical_digest
from tests.phase7.test_gateway_typescript_contract import _run_typescript


ROOT = Path(__file__).resolve().parents[2]


def test_v8_schema_models_one_immutable_command_per_dispatch() -> None:
    schema = (ROOT / "tools/runtime/conversation-runtime.sql").read_text(
        encoding="utf-8"
    )
    table_start = schema.index("CREATE TABLE IF NOT EXISTS waje_runtime.run_dispatches")
    table_end = schema.index("\n);", table_start)
    table = schema[table_start:table_end]

    assert (
        "CHECK (producer_kind IN ('thread_message', 'clarification_resolution'))"
        in table
    )
    assert "run_id text NOT NULL REFERENCES" in table
    assert "run_id text NOT NULL UNIQUE REFERENCES" not in table
    assert "message_id text NOT NULL UNIQUE REFERENCES" in table
    assert "UNIQUE(producer_kind, scope_ref, request_identity)" in table
    assert "run_dispatch_scope_shape_check" in table
    assert "run_dispatch_request_digest_check" in table
    assert "run_dispatch_request_payload_check" in table
    assert "idx_run_dispatch_one_active_per_run" in schema
    assert "WHERE dispatch_state IN ('pending', 'leased', 'running')" in schema
    assert "enforce_run_dispatch_command_immutable" in schema


def test_memory_store_supports_exactly_addressed_multi_dispatch_runs() -> None:
    result = _run_typescript(
        textwrap.dedent(
            """
            const store = await import("./app/api/_conversationStore.ts");
            const thread = await store.createThread("dispatch-v8-owner");
            const initialInput = {
              producerKind: "thread_message",
              scopeRef: thread.id,
              requestIdentity: "request-initial",
              threadId: thread.id,
              text: "分析昨天收入",
              actorId: "dispatch-v8-owner",
              requestPayload: { message: "分析昨天收入" },
            };
            const initial = await store.claimRunDispatchRequest(initialInput);
            const initialReplay = await store.claimRunDispatchRequest(initialInput);
            const initialLease = await store.acquireRunDispatchLease({
              dispatchId: initial.dispatch.dispatchId,
              runId: initial.run.id,
            });
            await store.completeOwnedRunDispatch({
              dispatchId: initial.dispatch.dispatchId,
              runId: initial.run.id,
              ownerId: initialLease.ownerId,
              leaseEpoch: initialLease.leaseEpoch,
              runStatus: "waiting_for_clarification",
            });

            const clarificationInput = {
              producerKind: "clarification_resolution",
              scopeRef: initial.run.id,
              requestIdentity: "request-clarification",
              threadId: thread.id,
              runId: initial.run.id,
              text: "采用上一日作为基线",
              actorId: "dispatch-v8-owner",
              requestPayload: {
                message: "采用上一日作为基线",
                clarification: {
                  sourceRunId: initial.run.id,
                  answer: "采用上一日作为基线",
                  selectedOptionId: "comparison_baseline.previous_day",
                },
              },
            };
            const clarification = await store.claimRunDispatchRequest(
              clarificationInput,
            );
            const replay = await store.claimRunDispatchRequest(clarificationInput);

            let digestConflict = null;
            try {
              await store.claimRunDispatchRequest({
                ...clarificationInput,
                text: "采用前七天均值作为基线",
                requestPayload: {
                  ...clarificationInput.requestPayload,
                  message: "采用前七天均值作为基线",
                },
              });
            } catch (error) {
              digestConflict = { code: error.code, status: error.httpStatus };
            }

            let activeConflict = null;
            try {
              await store.claimRunDispatchRequest({
                ...clarificationInput,
                requestIdentity: "request-clarification-second",
              });
            } catch (error) {
              activeConflict = { code: error.code, status: error.httpStatus };
            }

            let locatorConflict = null;
            try {
              await store.acquireRunDispatchLease({
                dispatchId: clarification.dispatch.dispatchId,
                runId: "run-wrong",
              });
            } catch (error) {
              locatorConflict = { code: error.code, status: error.httpStatus };
            }

            const firstLease = await store.acquireRunDispatchLease({
              dispatchId: clarification.dispatch.dispatchId,
              runId: initial.run.id,
            });
            const dispatch = globalThis.__wajeConversationMemoryStore
              .runDispatches.get(clarification.dispatch.dispatchId);
            dispatch.leaseExpiresAt = "2000-01-01T00:00:00.000Z";
            const replacementLease = await store.acquireRunDispatchLease({
              dispatchId: clarification.dispatch.dispatchId,
              runId: initial.run.id,
            });
            const stale = await store.failOwnedRunDispatch({
              dispatchId: clarification.dispatch.dispatchId,
              runId: initial.run.id,
              ownerId: firstLease.ownerId,
              leaseEpoch: firstLease.leaseEpoch,
              failureReason: "stale-owner",
            });
            const completed = await store.completeOwnedRunDispatch({
              dispatchId: clarification.dispatch.dispatchId,
              runId: initial.run.id,
              ownerId: replacementLease.ownerId,
              leaseEpoch: replacementLease.leaseEpoch,
              runStatus: "planned",
            });
            const memory = globalThis.__wajeConversationMemoryStore;
            console.log(JSON.stringify({
              initialRunId: initial.run.id,
              threadId: thread.id,
              clarificationRunId: clarification.run.id,
              initialDispatchId: initial.dispatch.dispatchId,
              initialReplayDispatchId: initialReplay.dispatch.dispatchId,
              initialReplayed: initialReplay.replayed,
              clarificationDispatchId: clarification.dispatch.dispatchId,
              clarificationDigest: clarification.dispatch.requestDigest,
              clarificationPayload: clarification.dispatch.requestPayload,
              leaseDispatchId: replacementLease.dispatchId,
              replayed: replay.replayed,
              replayDispatchId: replay.dispatch.dispatchId,
              digestConflict,
              activeConflict,
              locatorConflict,
              staleStatus: stale.status,
              completedStatus: completed.status,
              dispatchState: dispatch.state,
              dispatchTerminalStatus: dispatch.terminalStatus,
              dispatchKeys: [...memory.runDispatches.keys()],
              messageCount: thread.messages.length,
            }));
            """
        )
    )

    assert result["initialRunId"] == result["clarificationRunId"]
    assert result["initialDispatchId"] != result["clarificationDispatchId"]
    assert result["initialReplayed"] is True
    assert result["initialReplayDispatchId"] == result["initialDispatchId"]
    assert result["clarificationDigest"] == canonical_digest(
        {
            "producer_kind": "clarification_resolution",
            "scope_ref": result["initialRunId"],
            "thread_id": result["threadId"],
            "request_payload": result["clarificationPayload"],
        }
    )
    assert result["leaseDispatchId"] == result["clarificationDispatchId"]
    assert result["replayed"] is True
    assert result["replayDispatchId"] == result["clarificationDispatchId"]
    assert result["digestConflict"] == {
        "code": "run_dispatch_conflict",
        "status": 409,
    }
    assert result["activeConflict"] == {
        "code": "run_dispatch_active_conflict",
        "status": 409,
    }
    assert result["locatorConflict"] == {
        "code": "run_dispatch_conflict",
        "status": 409,
    }
    assert result["staleStatus"] == "waiting_for_clarification"
    assert result["completedStatus"] == "planned"
    assert result["dispatchState"] == "terminal"
    assert result["dispatchTerminalStatus"] == "planned"
    assert set(result["dispatchKeys"]) == {
        result["initialDispatchId"],
        result["clarificationDispatchId"],
    }
    assert result["messageCount"] == 2


def test_clarification_worker_exit_releases_the_exact_dispatch_for_recovery() -> None:
    result = _run_typescript(
        textwrap.dedent(
            """
            const store = await import("./app/api/_conversationStore.ts");
            const thread = await store.createThread("dispatch-recovery-owner");
            const initial = await store.claimRunDispatchRequest({
              producerKind: "thread_message",
              scopeRef: thread.id,
              requestIdentity: "request-recovery-initial",
              threadId: thread.id,
              text: "分析昨天收入",
              actorId: "dispatch-recovery-owner",
              requestPayload: { message: "分析昨天收入" },
            });
            const initialLease = await store.acquireRunDispatchLease({
              dispatchId: initial.dispatch.dispatchId,
              runId: initial.run.id,
            });
            await store.completeOwnedRunDispatch({
              dispatchId: initial.dispatch.dispatchId,
              runId: initial.run.id,
              ownerId: initialLease.ownerId,
              leaseEpoch: initialLease.leaseEpoch,
              runStatus: "waiting_for_clarification",
            });
            const resolution = await store.claimRunDispatchRequest({
              producerKind: "clarification_resolution",
              scopeRef: initial.run.id,
              requestIdentity: "request-recovery-resolution",
              threadId: thread.id,
              runId: initial.run.id,
              text: "采用上一日作为基线",
              actorId: "dispatch-recovery-owner",
              requestPayload: {
                message: "采用上一日作为基线",
                clarification: {
                  sourceRunId: initial.run.id,
                  resolutionId: "single-authority:request-recovery-resolution",
                  attemptRunId: initial.run.id,
                  answer: "采用上一日作为基线",
                  selectedOptionId: "comparison_baseline.previous_day",
                  source: "user",
                  retryAttempt: false,
                },
              },
            });
            const firstLease = await store.acquireRunDispatchLease({
              dispatchId: resolution.dispatch.dispatchId,
              runId: initial.run.id,
            });
            const firstObservation = await store.observeOwnedRunDispatchExit({
              dispatchId: resolution.dispatch.dispatchId,
              runId: initial.run.id,
              ownerId: firstLease.ownerId,
              leaseEpoch: firstLease.leaseEpoch,
              failureReason: "agent_core_worker_exited",
            });
            await store.observeOwnedRunDispatchExit({
              dispatchId: resolution.dispatch.dispatchId,
              runId: initial.run.id,
              ownerId: firstLease.ownerId,
              leaseEpoch: firstLease.leaseEpoch,
              failureReason: "agent_core_worker_exited",
            });
            const replacementLease = await store.acquireRunDispatchLease({
              dispatchId: resolution.dispatch.dispatchId,
              runId: initial.run.id,
            });
            await store.observeOwnedRunDispatchExit({
              dispatchId: resolution.dispatch.dispatchId,
              runId: initial.run.id,
              ownerId: firstLease.ownerId,
              leaseEpoch: firstLease.leaseEpoch,
              failureReason: "stale_owner_exit",
            });
            const durableFailure = await store.failOwnedRunDispatch({
              dispatchId: resolution.dispatch.dispatchId,
              runId: initial.run.id,
              ownerId: replacementLease.ownerId,
              leaseEpoch: replacementLease.leaseEpoch,
              failureReason: "clarification_command_invalid",
            });
            const memory = globalThis.__wajeConversationMemoryStore;
            const dispatch = memory.runDispatches.get(
              resolution.dispatch.dispatchId,
            );
            console.log(JSON.stringify({
              firstObservationStatus: firstObservation.status,
              durableFailureStatus: durableFailure.status,
              firstEpoch: firstLease.leaseEpoch,
              replacementEpoch: replacementLease.leaseEpoch,
              dispatch,
              recoveryAuditCount: memory.auditEvents.filter(
                (event) => event.eventType === "run_dispatch_recovery_requested"
              ).length,
              failedAuditCount: memory.auditEvents.filter(
                (event) => event.eventType === "run_dispatch_failed"
              ).length,
            }));
            """
        )
    )

    assert result["firstObservationStatus"] == "waiting_for_clarification"
    assert result["durableFailureStatus"] == "waiting_for_clarification"
    assert result["replacementEpoch"] == result["firstEpoch"] + 1
    assert result["dispatch"]["state"] == "terminal"
    assert result["dispatch"]["terminalStatus"] == "failed"
    assert result["dispatch"]["failureReason"] == ("clarification_command_invalid")
    assert result["recoveryAuditCount"] == 1
    assert result["failedAuditCount"] == 1


def test_dispatch_digest_rejects_envelopes_python_cannot_reproduce_exactly() -> None:
    result = _run_typescript(
        textwrap.dedent(
            """
            const store = await import("./app/api/_conversationStore.ts");
            const thread = await store.createThread("dispatch-digest-owner");
            const base = {
              producerKind: "thread_message",
              scopeRef: thread.id,
              requestIdentity: "request-digest",
              threadId: thread.id,
              text: "检查收入",
              actorId: "dispatch-digest-owner",
              requestPayload: { message: "检查收入" },
            };
            const errors = [];
            for (const input of [
              { ...base, scopeRef: "thread-wrong" },
              { ...base, requestPayload: { message: "检查成本" } },
              {
                ...base,
                requestPayload: { message: "检查收入", threshold: 1.5 },
              },
            ]) {
              try {
                await store.claimRunDispatchRequest(input);
              } catch (error) {
                errors.push({ code: error.code, status: error.httpStatus });
              }
            }
            console.log(JSON.stringify(errors));
            """
        )
    )

    assert result == [
        {"code": "run_dispatch_request_invalid", "status": 400},
        {"code": "run_dispatch_request_invalid", "status": 400},
        {"code": "run_dispatch_request_invalid", "status": 400},
    ]
