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
    const clarification = await recordClarificationOutcome(runId, answer);
    const agentCore = await runAgentCore(
      run.threadId,
      runId,
      answer,
      process.env.WAJE_GATEWAY_ROLE || "analyst",
    );
    return NextResponse.json({
      runId,
      message,
      clarification,
      agentCore,
      eventsUrl: `/api/runs/${run.id}/events`,
    });
  } catch (error) {
    return jsonError(error);
  }
}
