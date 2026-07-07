import { NextRequest, NextResponse } from "next/server";

import { jsonError, requireRun } from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ runId: string }> };

export async function POST(request: NextRequest, context: RouteContext) {
  const { runId } = await context.params;
  try {
    await requireRun(runId);
  } catch (error) {
    return jsonError(error);
  }
  const body = await request.json().catch(() => ({}));
  return NextResponse.json({
    runId,
    clarification: {
      answer: body.answer ?? body.choice ?? "",
      status: "accepted",
    },
  });
}
