import { NextRequest, NextResponse } from "next/server";

import { runAgentCore } from "../../../_agentCore";
import {
  addUserMessage,
  createRun,
  jsonError,
  requireArtifactForContinue,
  requireThread,
} from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ artifactId: string }> };

export async function POST(request: NextRequest, context: RouteContext) {
  const { artifactId } = await context.params;
  const body = await request.json().catch(() => ({}));
  const role = process.env.WAJE_GATEWAY_ROLE || "analyst";
  const message = typeof body.message === "string" ? body.message : "基于这个结果继续分析";
  try {
    const artifact = await requireArtifactForContinue(artifactId, role);
    const threadId = typeof body.threadId === "string" ? body.threadId : artifact.threadId;
    if (threadId !== artifact.threadId) {
      throw new Error("artifact_thread_mismatch");
    }
    await requireThread(threadId);
    const userMessage = await addUserMessage(threadId, message);
    const run = await createRun(threadId);
    const agentCore = await runAgentCore(threadId, run.id, message, role);
    const effectiveRun = agentCore?.status === "waiting_for_clarification"
      ? { ...run, status: "waiting_for_clarification" as const }
      : run;
    return NextResponse.json(
      {
        artifactId,
        artifact,
        message: userMessage,
        run: effectiveRun,
        agentCore,
        eventsUrl: `/api/runs/${run.id}/events`,
      },
      { status: 202 },
    );
  } catch (error) {
    return jsonError(error);
  }
}
