import { NextRequest, NextResponse } from "next/server";

import { runAgentCore } from "../../../_agentCore";
import { resolveCustomerActor } from "../../../_customerActor";
import {
  acquireRunDispatchLease,
  claimRunDispatchRequest,
  customerJsonError,
  gatewayError,
  loadCustomerAnalysisSnapshot,
  observeOwnedRunDispatchExit,
  requireRun,
  runDispatchRequestIdentity,
} from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ runId: string }> };

type OwnedDispatch = {
  dispatchId: string;
  runId: string;
  ownerId: string;
  leaseEpoch: number;
};

export async function POST(request: NextRequest, context: RouteContext) {
  const { runId } = await context.params;
  const body = await request.json().catch(() => ({}));
  let actorId: string | undefined;
  if (
    body === null
    || typeof body !== "object"
    || Array.isArray(body)
    || Object.keys(body).length !== 3
    || !Object.prototype.hasOwnProperty.call(body, "answer")
    || !Object.prototype.hasOwnProperty.call(body, "selectedOptionId")
    || !Object.prototype.hasOwnProperty.call(body, "requestIdentity")
  ) {
    return customerJsonError(gatewayError("clarification_request_invalid"), { runId });
  }
  const rawAnswer = body.answer;
  if (typeof rawAnswer !== "string" || !rawAnswer.trim()) {
    return customerJsonError(gatewayError("clarification_answer_required"), { runId });
  }
  const rawSelectedOptionId = body.selectedOptionId;
  if (
    rawSelectedOptionId !== undefined
    && rawSelectedOptionId !== null
    && (
      typeof rawSelectedOptionId !== "string"
      || !rawSelectedOptionId.trim()
    )
  ) {
    return customerJsonError(
      gatewayError("clarification_selected_option_invalid"),
      { runId },
    );
  }
  const answer = rawAnswer.trim();
  const selectedOptionId = typeof rawSelectedOptionId === "string"
    ? rawSelectedOptionId.trim()
    : null;
  let ownedDispatch: OwnedDispatch | null = null;

  try {
    actorId = resolveCustomerActor(request);
    const requestIdentity = runDispatchRequestIdentity(request, body);
    const run = await requireRun(runId, actorId);
    const clarification = {
      sourceRunId: runId,
      resolutionId: `single-authority:${requestIdentity}`,
      attemptRunId: runId,
      answer,
      selectedOptionId,
      source: "user" as const,
      retryAttempt: false,
    };
    const claim = await claimRunDispatchRequest({
      producerKind: "clarification_resolution",
      scopeRef: runId,
      requestIdentity,
      threadId: run.threadId,
      runId,
      text: answer,
      actorId,
      requestPayload: {
        message: answer,
        clarification,
      },
    });
    const dispatch = await acquireRunDispatchLease({
      dispatchId: claim.dispatch.dispatchId,
      runId,
    });
    if (!dispatch.acquired || !dispatch.ownerId) {
      return NextResponse.json(
        {
          snapshot: await loadCustomerAnalysisSnapshot({
            threadId: run.threadId,
            actorId,
            runId,
          }),
        },
        { status: 202 },
      );
    }

    ownedDispatch = {
      dispatchId: dispatch.dispatchId,
      runId,
      ownerId: dispatch.ownerId,
      leaseEpoch: dispatch.leaseEpoch,
    };
    const workerOwnership = ownedDispatch;
    const agentCore = await runAgentCore(
      run.threadId,
      runId,
      answer,
      actorId,
      {
        clarification,
        runDispatch: {
          dispatchId: workerOwnership.dispatchId,
          ownerId: workerOwnership.ownerId,
          leaseEpoch: workerOwnership.leaseEpoch,
        },
        onDetachedWorkerExit: () => observeOwnedRunDispatchExit({
          ...workerOwnership,
          failureReason: "agent_core_worker_exited",
        }).then(() => undefined),
      },
    );
    if (agentCore.error) throw gatewayError(agentCore.error);
    if (agentCore.status !== "started") {
      throw gatewayError("agent_core_output_status_invalid");
    }
    ownedDispatch = null;

    return NextResponse.json(
      {
        snapshot: await loadCustomerAnalysisSnapshot({
          threadId: run.threadId,
          actorId,
          runId,
        }),
      },
      { status: 202 },
    );
  } catch (error) {
    if (ownedDispatch) {
      const failureReason = error instanceof Error && error.message
        ? error.message
        : "agent_core_process_failed";
      await observeOwnedRunDispatchExit({
        ...ownedDispatch,
        failureReason,
      });
    }
    return customerJsonError(error, { actorId, runId });
  }
}
