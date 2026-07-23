import { NextRequest, NextResponse } from "next/server";

import {
  claimInitialThreadRequest,
  customerJsonError,
  listCustomerThreadSummaries,
  loadCustomerAnalysisSnapshot,
  runDispatchRequestIdentity,
  withCustomerActorScope,
} from "../_conversationStore";
import { resolveCustomerActor } from "../_customerActor";
import { gatewayError } from "../_conversationStore";
import { readBoundedCustomerJson } from "../_requestBudget";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: NextRequest) {
  let actorId: string | undefined;
  try {
    actorId = resolveCustomerActor(request);
    return withCustomerActorScope(actorId, async () => NextResponse.json({
      threads: await listCustomerThreadSummaries(actorId!),
    }));
  } catch (error) {
    return customerJsonError(error, { actorId });
  }
}

export async function POST(request: NextRequest) {
  let actorId: string | undefined;
  try {
    actorId = resolveCustomerActor(request);
    const body = await readBoundedCustomerJson(request, "thread_request_invalid");
    if ("ownerId" in body) {
      throw gatewayError("thread_owner_input_forbidden");
    }
    if (Object.keys(body).some((key) => key !== "requestIdentity")) {
      throw gatewayError("thread_request_invalid");
    }
    const requestIdentity = runDispatchRequestIdentity(request, body);
    const thread = await claimInitialThreadRequest(actorId!, requestIdentity);
    return NextResponse.json(
      {
        snapshot: await withCustomerActorScope(actorId, () =>
          loadCustomerAnalysisSnapshot({
            threadId: thread.id,
            actorId: actorId!,
          })
        ),
      },
      { status: 201 },
    );
  } catch (error) {
    return customerJsonError(error, { actorId });
  }
}
