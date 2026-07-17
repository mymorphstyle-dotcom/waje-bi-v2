import { NextRequest, NextResponse } from "next/server";

import { runAgentCore } from "../../../_agentCore";
import { resolveCustomerActor } from "../../../_customerActor";
import {
  acquireRunDispatchLease,
  claimClarificationRetryAttempt,
  completeOwnedRunDispatch,
  failOwnedRunDispatch,
  gatewayError,
  jsonError,
  projectAgentCoreForCustomer,
  runDispatchRequestIdentity,
} from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ runId: string }> };

export async function POST(request: NextRequest, context: RouteContext) {
  const { runId: failedRunId } = await context.params;
  const body = await request.json().catch(() => ({})) as Record<string, unknown>;
  if (
    Object.prototype.hasOwnProperty.call(body, "answer")
    || Object.prototype.hasOwnProperty.call(body, "choice")
    || Object.prototype.hasOwnProperty.call(body, "selectedOptionId")
  ) {
    return NextResponse.json(
      { error: "clarification_retry_submission_forbidden" },
      { status: 400 },
    );
  }
  let claimedRunId = "";
  let dispatchOwnerId = "";
  let dispatchLeaseEpoch = 0;
  let dispatchAcquired = false;
  try {
    const actorId = resolveCustomerActor(request);
    const requestIdentity = runDispatchRequestIdentity(request, body);
    const claim = await claimClarificationRetryAttempt({
      failedRunId,
      requestIdentity,
      actorId,
    });
    claimedRunId = claim.attemptRunId;
    const clarificationPayload = {
      sourceRunId: claim.sourceRunId,
      resolutionId: claim.resolutionId,
      attemptRunId: claim.attemptRunId,
      answer: claim.answer,
      selectedOptionId: claim.selectedOptionId,
      source: "user" as const,
      retryAttempt: true,
    };
    const clarification = {
      ...clarificationPayload,
      threadId: claim.threadId,
      topicId: claim.topicId,
      status: "accepted",
      requestIdentity: claim.requestIdentity,
      attemptNumber: claim.attemptNumber,
      previousAttemptRunId: claim.previousAttemptRunId,
    };
    const dispatch = await acquireRunDispatchLease({
      runId: claim.attemptRunId,
      requestIdentity: claim.requestIdentity,
    });
    if (!dispatch.acquired || !dispatch.ownerId) {
      return NextResponse.json({
        sourceRunId: claim.sourceRunId,
        resolutionId: claim.resolutionId,
        attemptRunId: claim.attemptRunId,
        previousAttemptRunId: claim.previousAttemptRunId,
        attemptNumber: claim.attemptNumber,
        topicId: claim.topicId,
        status: dispatch.run.status,
        answerPackagePreview: null,
        message: null,
        clarification,
        agentCore: {
          status: dispatch.reason === "active_lease"
            ? "dispatch_in_progress"
            : "replayed",
        },
        eventsUrl: `/api/runs/${claim.attemptRunId}/events`,
      });
    }
    dispatchAcquired = true;
    dispatchOwnerId = dispatch.ownerId;
    dispatchLeaseEpoch = dispatch.leaseEpoch;
    const agentCore = await runAgentCore(
      claim.threadId,
      claim.attemptRunId,
      claim.answer,
      actorId,
      {
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
        runId: claim.attemptRunId,
        ownerId: dispatch.ownerId,
        leaseEpoch: dispatch.leaseEpoch,
        failureReason: agentCore.error,
      });
      throw gatewayError(agentCore.error);
    }
    const rawResult = agentCore.result && typeof agentCore.result === "object"
      ? agentCore.result as Record<string, unknown>
      : {};
    if (rawResult.run_id !== claim.attemptRunId) {
      await failOwnedRunDispatch({
        runId: claim.attemptRunId,
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
      runId: claim.attemptRunId,
      ownerId: dispatch.ownerId,
      leaseEpoch: dispatch.leaseEpoch,
      runStatus: rawStatus,
    });
    const visibleAgentCore = projectAgentCoreForCustomer(
      agentCore as unknown as Record<string, unknown>,
    );
    const visibleResult = visibleAgentCore.result
      && typeof visibleAgentCore.result === "object"
      ? visibleAgentCore.result as Record<string, unknown>
      : {};
    return NextResponse.json({
      sourceRunId: claim.sourceRunId,
      resolutionId: claim.resolutionId,
      attemptRunId: claim.attemptRunId,
      previousAttemptRunId: claim.previousAttemptRunId,
      attemptNumber: claim.attemptNumber,
      topicId: visibleResult.topic_id ?? claim.topicId,
      status: visibleAgentCore.status,
      answerPackagePreview: visibleResult.answer_package ?? null,
      message: null,
      clarification,
      agentCore: visibleAgentCore,
      eventsUrl: `/api/runs/${claim.attemptRunId}/events`,
    });
  } catch (error) {
    if (claimedRunId && dispatchAcquired) {
      const failureReason = error instanceof Error && error.message
        ? error.message
        : "agent_core_process_failed";
      await failOwnedRunDispatch({
        runId: claimedRunId,
        ownerId: dispatchOwnerId,
        leaseEpoch: dispatchLeaseEpoch,
        failureReason,
      }).catch(() => undefined);
    }
    return jsonError(error);
  }
}
