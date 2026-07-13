import { NextRequest, NextResponse } from "next/server";

import { runAgentCore } from "../../../_agentCore";
import {
  addUserMessage,
  createMemoryProposal,
  createRun,
  filterAgentCoreForRole,
  jsonError,
  resolveGatewayRole,
} from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ threadId: string }> };

export async function POST(request: NextRequest, context: RouteContext) {
  const { threadId } = await context.params;
  const body = await request.json().catch(() => ({}));
  const text = typeof body.message === "string" ? body.message : "";
  if (!text.trim()) return NextResponse.json({ error: "message_required" }, { status: 400 });
  const roleDecision = resolveGatewayRole(
    process.env.WAJE_GATEWAY_ROLE,
    process.env.NODE_ENV,
  );

  try {
    const message = await addUserMessage(threadId, text);
    const memoryProposal = text.includes("记住") || text.includes("以后默认")
      ? await createMemoryProposal(threadId, text)
      : null;
    const run = memoryProposal ? null : await createRun(threadId);
    const agentCore = run
      ? await runAgentCore(
          threadId,
          run.id,
          text,
          roleDecision.displayRole,
          { runtimePermissionScope: roleDecision.runtimePermissionScope },
        )
      : null;
    const visibleAgentCore = agentCore
      ? filterAgentCoreForRole(
          agentCore as unknown as Record<string, unknown>,
          roleDecision.displayRole,
        )
      : null;
    const effectiveRun = run && agentCore?.status === "waiting_for_clarification"
      ? { ...run, status: "waiting_for_clarification" as const }
      : run;
    return NextResponse.json(
      {
        message,
        run: effectiveRun,
        memoryProposal,
        agentCore: visibleAgentCore,
        eventsUrl: run ? `/api/runs/${run.id}/events` : null,
      },
      { status: 202 },
    );
  } catch (error) {
    return jsonError(error);
  }
}
