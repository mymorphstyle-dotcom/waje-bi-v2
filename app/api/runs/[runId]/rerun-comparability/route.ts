import { jsonError, runRerunComparability } from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ runId: string }> };

export async function GET(request: Request, context: RouteContext) {
  const { runId } = await context.params;
  const candidateRunId = new URL(request.url).searchParams.get("candidateRunId");
  if (!candidateRunId) return Response.json({ error: "candidateRunId_required" }, { status: 400 });
  try {
    return Response.json(await runRerunComparability(runId, candidateRunId));
  } catch (error) {
    return jsonError(error);
  }
}
