import { jsonError, runAuditTrace } from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ runId: string }> };

export async function GET(_request: Request, context: RouteContext) {
  const { runId } = await context.params;
  try {
    return Response.json(await runAuditTrace(runId));
  } catch (error) {
    return jsonError(error);
  }
}
