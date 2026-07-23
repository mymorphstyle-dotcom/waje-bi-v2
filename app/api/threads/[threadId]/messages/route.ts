import { NextRequest, NextResponse } from "next/server";

import {
  type PendingActionResolutionInput,
  runGeneralAgentTurn,
} from "../../../_generalAgent";
import { resolveCustomerActor } from "../../../_customerActor";
import {
  customerJsonError,
  gatewayError,
  loadCustomerAnalysisSnapshot,
  requireThread,
  agentTurnRequestIdentity,
  withCustomerActorScope,
} from "../../../_conversationStore";
import {
  readBoundedCustomerJson,
  requireCustomerMessageBudget,
} from "../../../_requestBudget";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ threadId: string }> };

export async function POST(request: NextRequest, context: RouteContext) {
  const { threadId } = await context.params;
  let actorId: string | undefined;
  try {
    const body = await readBoundedCustomerJson(request, "message_request_invalid");
    const text = typeof body.message === "string" ? body.message : "";
    const message = text.trim();
    if (!message) throw gatewayError("message_required");
    requireCustomerMessageBudget(message);
    validateMessageBodyShape(body);
    actorId = resolveCustomerActor(request);
    await withCustomerActorScope(actorId, async () => {
      await requireThread(threadId, actorId!);
    });
    const operationId = agentTurnRequestIdentity(request, body);
    const pendingActionResolution = pendingActionResolutionFrom(
      body.pendingActionResolution,
      message,
    );
    const agent = await runGeneralAgentTurn({
      threadId,
      actorId,
      operationId,
      message,
      ...(pendingActionResolution ? { pendingActionResolution } : {}),
    });
    if (agent.error) throw gatewayError(agent.error, agent.technicalDetailRef);
    return NextResponse.json(
      {
        snapshot: await withCustomerActorScope(actorId, () =>
          loadCustomerAnalysisSnapshot({ threadId, actorId: actorId! })
        ),
      },
      { status: 202 },
    );
  } catch (error) {
    return customerJsonError(error, { actorId, threadId });
  }
}

function validateMessageBodyShape(body: Record<string, unknown>) {
  const allowed = new Set([
    "message",
    "requestIdentity",
    "pendingActionResolution",
  ]);
  if (Object.keys(body).some((key) => !allowed.has(key))) {
    throw gatewayError("message_request_invalid");
  }
}

function pendingActionResolutionFrom(
  value: unknown,
  message: string,
): PendingActionResolutionInput | undefined {
  if (value === undefined) return undefined;
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw gatewayError("pending_action_resolution_invalid");
  }
  const resolution = value as Record<string, unknown>;
  const allowed = new Set([
    "actionRef",
    "decision",
    "selectedOptionId",
    "answerText",
  ]);
  if (
    Object.keys(resolution).some((key) => !allowed.has(key))
    || !isExactText(resolution.actionRef)
    || !["answered", "approved", "rejected"].includes(String(resolution.decision))
    || !(resolution.selectedOptionId === null || resolution.selectedOptionId === undefined
      || isExactText(resolution.selectedOptionId))
    || !isExactText(resolution.answerText)
    || resolution.answerText !== message
  ) {
    throw gatewayError("pending_action_resolution_invalid");
  }
  return {
    actionRef: resolution.actionRef,
    decision: resolution.decision as PendingActionResolutionInput["decision"],
    selectedOptionId: typeof resolution.selectedOptionId === "string"
      ? resolution.selectedOptionId
      : null,
    answerText: resolution.answerText,
  };
}

function isExactText(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value === value.trim();
}
