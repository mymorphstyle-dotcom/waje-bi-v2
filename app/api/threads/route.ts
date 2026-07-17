import { NextRequest, NextResponse } from "next/server";

import { createThread, listThreads } from "../_conversationStore";
import { resolveCustomerActor } from "../_customerActor";
import { gatewayError, jsonError } from "../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: NextRequest) {
  try {
    const actorId = resolveCustomerActor(request);
    return NextResponse.json({ threads: await listThreads(actorId) });
  } catch (error) {
    return jsonError(error);
  }
}

export async function POST(request: NextRequest) {
  try {
    const actorId = resolveCustomerActor(request);
    const body = await request.json().catch(() => ({}));
    if (body && typeof body === "object" && "ownerId" in body) {
      throw gatewayError("thread_owner_input_forbidden");
    }
    const thread = await createThread(actorId);
    return NextResponse.json({ thread }, { status: 201 });
  } catch (error) {
    return jsonError(error);
  }
}
