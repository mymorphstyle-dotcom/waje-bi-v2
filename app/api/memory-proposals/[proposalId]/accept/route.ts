import { NextResponse } from "next/server";

import { jsonError, updateMemoryProposal } from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ proposalId: string }> };

export async function POST(_request: Request, context: RouteContext) {
  const { proposalId } = await context.params;
  try {
    return NextResponse.json({ memoryProposal: await updateMemoryProposal(proposalId, "accepted") });
  } catch (error) {
    return jsonError(error);
  }
}
