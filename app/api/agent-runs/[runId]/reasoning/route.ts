import { NextResponse } from "next/server";

import {
  jsonError,
  loadPersistedRunReasoning,
} from "../../../_conversationStore";
import { assertInternalRouteAvailable } from "../../../_customerActor";
import { traceReasoningFromPersistedState } from "../../../_runReasoningProjection";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ runId: string }> };

export async function GET(_request: Request, context: RouteContext) {
  try {
    assertInternalRouteAvailable();
    const { runId } = await context.params;
    const persisted = await loadPersistedRunReasoning(runId);
    if (!persisted) {
      return NextResponse.json({ reasoning: null });
    }
    return NextResponse.json({
      reasoning: traceReasoningFromPersistedState(persisted),
    });
  } catch (error) {
    return jsonError(error);
  }
}
