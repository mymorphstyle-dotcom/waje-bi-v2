import { NextRequest, NextResponse } from "next/server";

import { runAgentCore } from "../../../_agentCore";
import { resolveCustomerActor } from "../../../_customerActor";
import {
  acquireRunDispatchLease,
  claimClarificationResolutionAttempt,
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
    const actorId = resolveCustomerActor(request);
    const requestIdentity = runDispatchRequestIdentity(request, body);
    const claim = await claimClarificationResolutionAttempt({
      sourceRunId: runId,
      requestIdentity,
      answer,
      selectedOptionId,
      source: "user",
      actorId,
    });
    claimedRunId = claim.attemptRunId;
    const clarificationPayload = {
      sourceRunId: claim.sourceRunId,
      resolutionId: claim.resolutionId,
      attemptRunId: claim.attemptRunId,
      answer,
      selectedOptionId,
      source: "user" as const,
      retryAttempt: false,
    };
    const clarification = {
      ...clarificationPayload,
      threadId: claim.threadId,
      status: "accepted",
      requestIdentity: claim.requestIdentity,
      attemptNumber: claim.attemptNumber,
    };
    const dispatch = await acquireRunDispatchLease({
      runId: claim.attemptRunId,
      requestIdentity: claim.requestIdentity,
    });
    if (!dispatch.acquired || !dispatch.ownerId) {
      return NextResponse.json({
        sourceRunId: runId,
        resolutionId: claim.resolutionId,
        attemptRunId: claim.attemptRunId,
        previousAttemptRunId: claim.previousAttemptRunId,
        attemptNumber: claim.attemptNumber,
        topicId: claim.topicId,
        status: dispatch.run.status,
        answerPackagePreview: null,
        message: claim.message,
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
      answer,
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
    const visibleResult = visibleAgentCore.result && typeof visibleAgentCore.result === "object"
      ? visibleAgentCore.result as Record<string, unknown>
      : {};
    const answerPackagePreview = visibleResult.answer_package ?? null;
    return NextResponse.json({
      sourceRunId: runId,
      resolutionId: claim.resolutionId,
      attemptRunId: claim.attemptRunId,
      previousAttemptRunId: claim.previousAttemptRunId,
      attemptNumber: claim.attemptNumber,
      topicId: visibleResult.topic_id ?? null,
      status: visibleAgentCore.status,
      answerPackagePreview,
      message: claim.message,
      clarification,
      agentCore: visibleAgentCore,
      eventsUrl: `/api/runs/${claim.attemptRunId}/events`,
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
