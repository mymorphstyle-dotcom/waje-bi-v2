import { customerJsonError, gatewayError, runRerunComparability, withCustomerActorScope } from "../../../_conversationStore";
import { resolveCustomerActor } from "../../../_customerActor";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ runId: string }> };

export async function GET(request: Request, context: RouteContext) {
  const { runId } = await context.params;
  const candidateRunId = new URL(request.url).searchParams.get("candidateRunId");
  let actorId: string | undefined;
  try {
    actorId = resolveCustomerActor(request);
    if (!candidateRunId) throw gatewayError("candidate_run_id_required");
    return withCustomerActorScope(actorId, async () => Response.json(
      await runRerunComparability(runId, candidateRunId, actorId!),
    ));
  } catch (error) {
    return customerJsonError(error, { actorId, runId });
  }
}
