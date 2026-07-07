import { conversationStore, jsonError } from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ runId: string }> };

export async function GET(_request: Request, context: RouteContext) {
  const { runId } = await context.params;
  const run = conversationStore().runs.get(runId);
  if (!run) return jsonError(new Error("run_not_found"));

  const events = [
    { event: "run_queued", runId, threadId: run.threadId },
    { event: "context_manifest_pending", runId },
  ];
  const body = events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
  return new Response(body, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
    },
  });
}
