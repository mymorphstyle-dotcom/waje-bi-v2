import { NextRequest, NextResponse } from "next/server";

import { addUserMessage, createRun, jsonError, requireThread } from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ artifactId: string }> };

export async function POST(request: NextRequest, context: RouteContext) {
  const { artifactId } = await context.params;
  const body = await request.json().catch(() => ({}));
  const threadId = typeof body.threadId === "string" ? body.threadId : "";
  const message = typeof body.message === "string" ? body.message : "基于这个结果继续分析";
  try {
    requireThread(threadId);
    const userMessage = addUserMessage(threadId, message);
    const run = createRun(threadId);
    return NextResponse.json(
      {
        artifactId,
        message: userMessage,
        run,
        eventsUrl: `/api/runs/${run.id}/events`,
      },
      { status: 202 },
    );
  } catch (error) {
    return jsonError(error);
  }
}
