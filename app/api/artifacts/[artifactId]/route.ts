import { NextResponse } from "next/server";

import { jsonError, readArtifactForRole } from "../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ artifactId: string }> };

export async function GET(_request: Request, context: RouteContext) {
  const { artifactId } = await context.params;
  try {
    const artifact = await readArtifactForRole(
      artifactId,
      process.env.WAJE_GATEWAY_ROLE || "analyst",
      "open",
    );
    return NextResponse.json({ artifact });
  } catch (error) {
    return jsonError(error);
  }
}
