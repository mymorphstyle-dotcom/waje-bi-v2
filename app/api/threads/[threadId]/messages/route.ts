import { NextRequest, NextResponse } from "next/server";

import { addUserMessage, createMemoryProposal, createRun, jsonError } from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ threadId: string }> };

export async function POST(request: NextRequest, context: RouteContext) {
  const { threadId } = await context.params;
  const body = await request.json().catch(() => ({}));
  const text = typeof body.message === "string" ? body.message : "";
  if (!text.trim()) return NextResponse.json({ error: "message_required" }, { status: 400 });

  try {
    const message = addUserMessage(threadId, text);
    const memoryProposal = text.includes("记住") || text.includes("以后默认")
      ? createMemoryProposal(threadId, text)
      : null;
    const run = memoryProposal ? null : createRun(threadId);
    return NextResponse.json(
      {
        message,
        run,
        memoryProposal,
        eventsUrl: run ? `/api/runs/${run.id}/events` : null,
      },
      { status: 202 },
    );
  } catch (error) {
    return jsonError(error);
  }
}
