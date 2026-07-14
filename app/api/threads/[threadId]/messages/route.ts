import { NextRequest, NextResponse } from "next/server";

import { runAgentCore } from "../../../_agentCore";
import {
  acquireRunDispatchLease,
  addUserMessage,
  claimRunDispatchRequest,
  completeOwnedRunDispatch,
  createMemoryProposal,
  failOwnedRunDispatch,
  filterAgentCoreForRole,
  gatewayError,
  jsonError,
  resolveGatewayRole,
  runDispatchRequestIdentity,
} from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ threadId: string }> };

export async function POST(request: NextRequest, context: RouteContext) {
  const { threadId } = await context.params;
  const body = await request.json().catch(() => ({})) as Record<string, unknown>;
  const text = typeof body.message === "string" ? body.message : "";
  if (!text.trim()) return NextResponse.json({ error: "message_required" }, { status: 400 });
  const roleDecision = resolveGatewayRole(
    process.env.WAJE_GATEWAY_ROLE,
    process.env.NODE_ENV,
  );
  let ownedDispatch: {
    runId: string;
    ownerId: string;
    leaseEpoch: number;
  } | null = null;

  try {
    const recordsMemory = text.includes("记住") || text.includes("以后默认");
    if (recordsMemory) {
      const message = await addUserMessage(threadId, text);
      const memoryProposal = await createMemoryProposal(threadId, text);
      return NextResponse.json(
        {
          message,
          run: null,
          memoryProposal,
          agentCore: null,
          eventsUrl: null,
        },
        { status: 202 },
      );
    }
    const requestIdentity = runDispatchRequestIdentity(request, body);
    const claim = await claimRunDispatchRequest({
      producerKind: "thread_message",
      scopeRef: threadId,
      requestIdentity,
      threadId,
      text,
      requestPayload: {
        message: text.trim(),
        runtimePermissionScope: roleDecision.runtimePermissionScope,
      },
    });
    const dispatch = await acquireRunDispatchLease({
      runId: claim.run.id,
      requestIdentity,
    });
    if (!dispatch.acquired || !dispatch.ownerId) {
      return NextResponse.json(
        {
          message: claim.message,
          run: dispatch.run,
          memoryProposal: null,
          agentCore: {
            status: dispatch.reason === "active_lease"
              ? "dispatch_in_progress"
              : "replayed",
          },
          eventsUrl: `/api/runs/${claim.run.id}/events`,
        },
        { status: 202 },
      );
    }
    const dispatchOwnerId = dispatch.ownerId;
    ownedDispatch = {
      runId: claim.run.id,
      ownerId: dispatchOwnerId,
      leaseEpoch: dispatch.leaseEpoch,
    };
    const agentCore = await runAgentCore(
      threadId,
      claim.run.id,
      text,
      roleDecision.displayRole,
      {
        runtimePermissionScope: roleDecision.runtimePermissionScope,
        runDispatch: {
          ownerId: dispatchOwnerId,
          leaseEpoch: dispatch.leaseEpoch,
        },
        onDetachedWorkerExit: () => failOwnedRunDispatch({
          runId: claim.run.id,
          ownerId: dispatchOwnerId,
          leaseEpoch: dispatch.leaseEpoch,
          failureReason: "agent_core_worker_exited",
        }).then(() => undefined),
      },
    );
    const visibleAgentCore = agentCore
      ? filterAgentCoreForRole(
          agentCore as unknown as Record<string, unknown>,
          roleDecision.displayRole,
        )
      : null;
    let effectiveRun = claim.run;
    const agentResult = agentCore?.result && typeof agentCore.result === "object"
      ? agentCore.result as Record<string, unknown>
      : null;
    const runIdMismatch = Boolean(
      agentResult?.run_id && agentResult.run_id !== claim.run.id
    );
    if (agentCore?.error || runIdMismatch) {
      effectiveRun = await failOwnedRunDispatch({
        runId: claim.run.id,
        ownerId: dispatch.ownerId,
        leaseEpoch: dispatch.leaseEpoch,
        failureReason: runIdMismatch
          ? "agent_core_run_id_mismatch"
          : agentCore?.error ?? "agent_core_process_failed",
      });
      ownedDispatch = null;
      throw gatewayError(
        runIdMismatch ? "agent_core_run_id_mismatch" : agentCore?.error ?? "agent_core_process_failed",
      );
    }
    const terminalStatus = agentResult?.status;
    if (
      terminalStatus === "completed"
      || terminalStatus === "completed_without_workflow"
      || terminalStatus === "waiting_for_clarification"
      || terminalStatus === "failed"
    ) {
      effectiveRun = await completeOwnedRunDispatch({
        runId: claim.run.id,
        ownerId: dispatch.ownerId,
        leaseEpoch: dispatch.leaseEpoch,
        runStatus: terminalStatus,
      });
      ownedDispatch = null;
    }
    return NextResponse.json(
      {
        message: claim.message,
        run: effectiveRun,
        memoryProposal: null,
        agentCore: visibleAgentCore,
        eventsUrl: `/api/runs/${claim.run.id}/events`,
      },
      { status: 202 },
    );
  } catch (error) {
    if (ownedDispatch) {
      const failureReason = error instanceof Error && error.message
        ? error.message
        : "agent_core_process_failed";
      await failOwnedRunDispatch({
        ...ownedDispatch,
        failureReason,
      }).catch(() => undefined);
    }
    return jsonError(error);
  }
}
