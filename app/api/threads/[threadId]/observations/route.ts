import { NextResponse } from "next/server";

import { resolveCustomerActor } from "../../../_customerActor";
import {
  customerJsonError,
  loadCustomerThreadCapabilityObservations,
  withCustomerActorScope,
} from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ threadId: string }> };

export async function GET(request: Request, context: RouteContext) {
  const { threadId } = await context.params;
  let actorId: string | undefined;
  try {
    actorId = resolveCustomerActor(request);
    const observations = await withCustomerActorScope(actorId, () =>
      loadCustomerThreadCapabilityObservations(threadId, actorId!)
    );
    return NextResponse.json({
      schemaVersion: "customer-capability-observations.v1",
      threadHandle: threadId,
      ...observations,
    });
  } catch (error) {
    return customerJsonError(error, { actorId, threadId });
  }
}
