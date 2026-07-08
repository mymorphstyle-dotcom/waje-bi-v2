import { NextRequest, NextResponse } from "next/server";

import { runAgentCore } from "../../../_agentCore";
import {
  addUserMessage,
  jsonError,
  recordClarificationOutcome,
  requireRun,
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
      source: "user",
    };
    const clarification = await recordClarificationOutcome(clarificationPayload);
    const agentCore = await runAgentCore(
      run.threadId,
      runId,
      answer,
      process.env.WAJE_GATEWAY_ROLE || "analyst",
    );
    const resumed = agentCore.result && typeof agentCore.result === "object"
      ? agentCore.result as Record<string, unknown>
      : {};
    return NextResponse.json({
      runId,
      resumedRunId: runId,
      topicId: resumed.topic_id ?? null,
      status: resumed.status ?? agentCore.status,
      answerPackagePreview: resumed.answer_package ?? null,
      message,
      clarification,
      agentCore,
      eventsUrl: `/api/runs/${run.id}/events`,
    });
  } catch (error) {
    return jsonError(error);
  }
}
