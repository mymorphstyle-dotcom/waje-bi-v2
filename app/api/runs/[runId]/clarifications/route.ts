import { NextRequest, NextResponse } from "next/server";

import { runAgentCore } from "../../../_agentCore";
import {
  acquireRunDispatchLease,
  claimClarificationResume,
  completeOwnedRunDispatch,
  failOwnedRunDispatch,
  filterAgentCoreForRole,
  gatewayError,
  jsonError,
  resolveGatewayRole,
  runDispatchRequestIdentity,
} from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ runId: string }> };

export async function POST(request: NextRequest, context: RouteContext) {
  const { runId } = await context.params;
  const body = await request.json().catch(() => ({}));
  const rawAnswer = body.answer ?? body.choice;
  if (typeof rawAnswer !== "string" || !rawAnswer.trim()) {
    return NextResponse.json({ error: "clarification_answer_required" }, { status: 400 });
  }
  const rawSelectedOptionId = body.selectedOptionId;
  if (
    rawSelectedOptionId !== undefined
    && rawSelectedOptionId !== null
    && (typeof rawSelectedOptionId !== "string" || !rawSelectedOptionId.trim())
  ) {
    return NextResponse.json(
      { error: "clarification_selected_option_invalid" },
      { status: 400 },
    );
  }
  const answer = rawAnswer.trim();
  const selectedOptionId = typeof rawSelectedOptionId === "string"
    ? rawSelectedOptionId.trim()
    : null;
  let claimedRunId = "";
  let dispatchAcquired = false;
  let dispatchOwnerId = "";
  let dispatchLeaseEpoch = 0;
  try {
    const roleDecision = resolveGatewayRole(
      process.env.WAJE_GATEWAY_ROLE,
      process.env.NODE_ENV,
    );
    const clarificationPayload = {
      runId,
      answer,
      selectedOptionId,
      source: "user" as const,
    };
    const requestIdentity = runDispatchRequestIdentity(request, body);
    const claim = await claimClarificationResume({
      sourceRunId: runId,
      requestIdentity,
      answer,
      selectedOptionId: clarificationPayload.selectedOptionId,
      source: clarificationPayload.source,
      runtimePermissionScope: roleDecision.runtimePermissionScope,
    });
    claimedRunId = claim.resumedRunId;
    const clarification = {
      ...clarificationPayload,
      threadId: claim.threadId,
      status: "accepted",
      requestIdentity: claim.requestIdentity,
      resumedRunId: claim.resumedRunId,
    };
    const dispatch = await acquireRunDispatchLease({
      runId: claim.resumedRunId,
      requestIdentity: claim.requestIdentity,
    });
    if (!dispatch.acquired || !dispatch.ownerId) {
      return NextResponse.json({
        sourceRunId: runId,
        resumedRunId: claim.resumedRunId,
        topicId: null,
        status: dispatch.run.status,
        answerPackagePreview: null,
        message: claim.message,
        clarification,
        agentCore: {
          status: dispatch.reason === "active_lease"
            ? "dispatch_in_progress"
            : "replayed",
        },
        eventsUrl: `/api/runs/${claim.resumedRunId}/events`,
      });
    }
    dispatchAcquired = true;
    dispatchOwnerId = dispatch.ownerId;
    dispatchLeaseEpoch = dispatch.leaseEpoch;
    const agentCore = await runAgentCore(
      claim.threadId,
      claim.resumedRunId,
      answer,
      roleDecision.displayRole,
      {
        runtimePermissionScope: roleDecision.runtimePermissionScope,
        clarification: clarificationPayload,
        runDispatch: {
          ownerId: dispatch.ownerId,
          leaseEpoch: dispatch.leaseEpoch,
        },
        forceInline: true,
      },
    );
    if (agentCore.error) {
      await failOwnedRunDispatch({
        runId: claim.resumedRunId,
        ownerId: dispatch.ownerId,
        leaseEpoch: dispatch.leaseEpoch,
        failureReason: agentCore.error,
      });
      throw gatewayError(agentCore.error);
    }
    const rawResult = agentCore.result && typeof agentCore.result === "object"
      ? agentCore.result as Record<string, unknown>
      : {};
    if (rawResult.run_id !== claim.resumedRunId) {
      await failOwnedRunDispatch({
        runId: claim.resumedRunId,
        ownerId: dispatch.ownerId,
        leaseEpoch: dispatch.leaseEpoch,
        failureReason: "agent_core_run_id_mismatch",
      });
      throw gatewayError("agent_core_run_id_mismatch");
    }
    const rawStatus = rawResult.status;
    if (
      rawStatus !== "completed"
      && rawStatus !== "completed_without_workflow"
      && rawStatus !== "waiting_for_clarification"
      && rawStatus !== "failed"
    ) {
      throw gatewayError("agent_core_output_status_invalid");
    }
    await completeOwnedRunDispatch({
      runId: claim.resumedRunId,
      ownerId: dispatch.ownerId,
      leaseEpoch: dispatch.leaseEpoch,
      runStatus: rawStatus,
    });
    const visibleAgentCore = filterAgentCoreForRole(
      agentCore as unknown as Record<string, unknown>,
      roleDecision.displayRole,
    );
    const visibleResult = visibleAgentCore.result && typeof visibleAgentCore.result === "object"
      ? visibleAgentCore.result as Record<string, unknown>
      : {};
    const answerPackagePreview = visibleResult.answer_package ?? null;
    return NextResponse.json({
      sourceRunId: runId,
      resumedRunId: claim.resumedRunId,
      topicId: visibleResult.topic_id ?? null,
      status: visibleAgentCore.status,
      answerPackagePreview,
      message: claim.message,
      clarification,
      agentCore: visibleAgentCore,
      eventsUrl: `/api/runs/${claim.resumedRunId}/events`,
    });
  } catch (error) {
    if (claimedRunId && dispatchAcquired) {
      const code = error instanceof Error && error.message
        ? error.message
        : "agent_core_process_failed";
      await failOwnedRunDispatch({
        runId: claimedRunId,
        ownerId: dispatchOwnerId,
        leaseEpoch: dispatchLeaseEpoch,
        failureReason: code,
      }).catch(() => undefined);
    }
    return jsonError(error);
  }
}
