import { NextRequest, NextResponse } from "next/server";

import { runAgentCore } from "../../../_agentCore";
import {
  addUserMessage,
  filterAgentCoreForRole,
  jsonError,
  recordClarificationOutcome,
  requireRun,
  resolveGatewayRole,
} from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ runId: string }> };

export async function POST(request: NextRequest, context: RouteContext) {
  const { runId } = await context.params;
  const body = await request.json().catch(() => ({}));
  const answer = String(body.answer ?? body.choice ?? "").trim();
  if (!answer) return NextResponse.json({ error: "clarification_answer_required" }, { status: 400 });
  try {
    const run = await requireRun(runId);
    const message = await addUserMessage(run.threadId, answer);
    const clarificationPayload = {
      runId,
      answer,
      selectedOptionId: body.selectedOptionId ?? null,
      source: "user" as const,
    };
    const clarification = await recordClarificationOutcome(clarificationPayload);
    const roleDecision = resolveGatewayRole(
      process.env.WAJE_GATEWAY_ROLE,
      process.env.NODE_ENV,
    );
    const agentCore = await runAgentCore(
      run.threadId,
      runId,
      answer,
      roleDecision.displayRole,
      {
        runtimePermissionScope: roleDecision.runtimePermissionScope,
        clarification: clarificationPayload,
        forceInline: true,
      },
    );
    const visibleAgentCore = filterAgentCoreForRole(
      agentCore as unknown as Record<string, unknown>,
      roleDecision.displayRole,
    );
    const visibleResult = visibleAgentCore.result && typeof visibleAgentCore.result === "object"
      ? visibleAgentCore.result as Record<string, unknown>
      : {};
    const answerPackagePreview = visibleResult.answer_package ?? null;
    return NextResponse.json({
      runId,
      resumedRunId: visibleResult.run_id ?? runId,
      topicId: visibleResult.topic_id ?? null,
      status: visibleAgentCore.status,
      answerPackagePreview,
      message,
      clarification,
      agentCore: visibleAgentCore,
      eventsUrl: `/api/runs/${run.id}/events`,
    });
  } catch (error) {
    return jsonError(error);
  }
}
