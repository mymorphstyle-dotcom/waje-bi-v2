import { customerJsonError, runAuditTrace, withCustomerActorScope } from "../../../_conversationStore";
import { resolveCustomerActor } from "../../../_customerActor";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ runId: string }> };

export async function GET(request: Request, context: RouteContext) {
  const { runId } = await context.params;
  let actorId: string | undefined;
  try {
    actorId = resolveCustomerActor(request);
    return withCustomerActorScope(actorId, async () => Response.json(
      await runAuditTrace(runId, actorId!),
    ));
  } catch (error) {
    return customerJsonError(error, { actorId, runId });
  }
}
