import { NextRequest, NextResponse } from "next/server";

import { conversationStore, jsonError } from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ runId: string }> };

export async function POST(request: NextRequest, context: RouteContext) {
  const { runId } = await context.params;
  const run = conversationStore().runs.get(runId);
  if (!run) return jsonError(new Error("run_not_found"));
  const body = await request.json().catch(() => ({}));
  return NextResponse.json({
    runId,
    clarification: {
      answer: body.answer ?? body.choice ?? "",
      status: "accepted",
    },
  });
}
