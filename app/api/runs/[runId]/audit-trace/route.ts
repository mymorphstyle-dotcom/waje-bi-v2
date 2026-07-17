import { jsonError, runAuditTrace } from "../../../_conversationStore";
import { resolveCustomerActor } from "../../../_customerActor";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ runId: string }> };

export async function GET(request: Request, context: RouteContext) {
  const { runId } = await context.params;
  try {
    const actorId = resolveCustomerActor(request);
    return Response.json(await runAuditTrace(runId, actorId));
  } catch (error) {
    return jsonError(error);
  }
}
