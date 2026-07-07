import { NextResponse } from "next/server";

import { jsonError, requireThread } from "../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ threadId: string }> };

export async function GET(_request: Request, context: RouteContext) {
  const { threadId } = await context.params;
  try {
    return NextResponse.json({ thread: requireThread(threadId) });
  } catch (error) {
    return jsonError(error);
  }
}
