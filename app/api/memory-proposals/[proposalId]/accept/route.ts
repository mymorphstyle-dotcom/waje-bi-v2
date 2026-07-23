import { NextResponse } from "next/server";

import { customerJsonError, updateMemoryProposal, withCustomerActorScope } from "../../../_conversationStore";
import { resolveCustomerActor } from "../../../_customerActor";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ proposalId: string }> };

export async function POST(request: Request, context: RouteContext) {
  const { proposalId } = await context.params;
  let actorId: string | undefined;
  try {
    actorId = resolveCustomerActor(request);
    return withCustomerActorScope(actorId, async () => NextResponse.json({
      memoryProposal: await updateMemoryProposal(proposalId, "accepted", actorId!),
    }));
  } catch (error) {
    return customerJsonError(error, { actorId });
  }
}
