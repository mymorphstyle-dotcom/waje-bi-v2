import { jsonError, runEvents } from "../../../_conversationStore";
import { resolveCustomerActor } from "../../../_customerActor";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ runId: string }> };

export async function GET(request: Request, context: RouteContext) {
  const { runId } = await context.params;
  let events;
  try {
    const actorId = resolveCustomerActor(request);
    events = await runEvents(runId, actorId);
  } catch (error) {
    return jsonError(error);
  }

  const body = events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
  return new Response(body, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
    },
  });
}
