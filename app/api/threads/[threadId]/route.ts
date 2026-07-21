import { NextResponse } from "next/server";

import {
  customerJsonError,
  loadCustomerAnalysisSnapshot,
} from "../../_conversationStore";
import { resolveCustomerActor } from "../../_customerActor";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ threadId: string }> };

export async function GET(request: Request, context: RouteContext) {
  const { threadId } = await context.params;
  let actorId: string | undefined;
  try {
    actorId = resolveCustomerActor(request);
    return NextResponse.json({
      snapshot: await loadCustomerAnalysisSnapshot({ threadId, actorId }),
    });
  } catch (error) {
    return customerJsonError(error, { actorId, threadId });
  }
}
