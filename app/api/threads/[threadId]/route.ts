import { NextResponse } from "next/server";

import { jsonError, requireThread } from "../../_conversationStore";
import { resolveCustomerActor } from "../../_customerActor";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ threadId: string }> };

export async function GET(request: Request, context: RouteContext) {
  const { threadId } = await context.params;
  try {
    const actorId = resolveCustomerActor(request);
    return NextResponse.json({ thread: await requireThread(threadId, actorId) });
  } catch (error) {
    return jsonError(error);
  }
}
