import { NextResponse } from "next/server";

import { jsonError, readArtifact } from "../../_conversationStore";
import { resolveCustomerActor } from "../../_customerActor";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ artifactId: string }> };

export async function GET(request: Request, context: RouteContext) {
  const { artifactId } = await context.params;
  try {
    const actorId = resolveCustomerActor(request);
    const artifact = await readArtifact(artifactId, actorId, "open");
    return NextResponse.json({ artifact });
  } catch (error) {
    return jsonError(error);
  }
}
