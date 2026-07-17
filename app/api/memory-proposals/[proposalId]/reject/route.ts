import { NextResponse } from "next/server";

import { jsonError, updateMemoryProposal } from "../../../_conversationStore";
import { resolveCustomerActor } from "../../../_customerActor";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ proposalId: string }> };

export async function POST(request: Request, context: RouteContext) {
  const { proposalId } = await context.params;
  try {
    const actorId = resolveCustomerActor(request);
    return NextResponse.json({ memoryProposal: await updateMemoryProposal(proposalId, "rejected", actorId) });
  } catch (error) {
    return jsonError(error);
  }
}
