import { NextRequest, NextResponse } from "next/server";

import {
  claimInitialThreadRequest,
  customerJsonError,
  listCustomerThreadSummaries,
  loadCustomerAnalysisSnapshot,
  runDispatchRequestIdentity,
} from "../_conversationStore";
import { resolveCustomerActor } from "../_customerActor";
import { gatewayError } from "../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: NextRequest) {
  let actorId: string | undefined;
  try {
    actorId = resolveCustomerActor(request);
    return NextResponse.json({
      threads: await listCustomerThreadSummaries(actorId),
    });
  } catch (error) {
    return customerJsonError(error, { actorId });
  }
}

export async function POST(request: NextRequest) {
  let actorId: string | undefined;
  try {
    actorId = resolveCustomerActor(request);
    const body = await request.json().catch(() => ({}));
    if (body === null || typeof body !== "object" || Array.isArray(body)) {
      throw gatewayError("thread_request_invalid");
    }
    if ("ownerId" in body) {
      throw gatewayError("thread_owner_input_forbidden");
    }
    if (Object.keys(body).some((key) => key !== "requestIdentity")) {
      throw gatewayError("thread_request_invalid");
    }
    const requestIdentity = runDispatchRequestIdentity(request, body);
    const thread = await claimInitialThreadRequest(actorId, requestIdentity);
    return NextResponse.json(
      {
        snapshot: await loadCustomerAnalysisSnapshot({
          threadId: thread.id,
          actorId,
        }),
      },
      { status: 201 },
    );
  } catch (error) {
    return customerJsonError(error, { actorId });
  }
}
