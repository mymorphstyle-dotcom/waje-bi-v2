import {
  customerJsonError,
  loadCustomerAnalysisSnapshot,
  requireRun,
} from "../../../_conversationStore";
import { resolveCustomerActor } from "../../../_customerActor";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ runId: string }> };

export async function GET(request: Request, context: RouteContext) {
  const { runId } = await context.params;
  let actorId: string | undefined;
  try {
    const resolvedActorId = resolveCustomerActor(request);
    actorId = resolvedActorId;
    const run = await requireRun(runId, resolvedActorId);
    const snapshot = await loadCustomerAnalysisSnapshot({
      threadId: run.threadId,
      actorId: resolvedActorId,
      runId,
    });
    const lastEventId = request.headers.get("last-event-id")?.trim();
    const encoder = new TextEncoder();
    const terminal = new Set(["completed", "completed_with_limits", "failed"]);
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        let closed = false;
        let current = snapshot;
        let deliveredVersion = lastEventId ?? "";
        let lastHeartbeatAt = Date.now();
        const close = () => {
          if (closed) return;
          closed = true;
          controller.close();
        };
        const send = (text: string) => controller.enqueue(encoder.encode(text));
        const sendSnapshot = () => {
          if (current.stateVersion === deliveredVersion) return;
          deliveredVersion = current.stateVersion;
          send([
            `id: ${current.stateVersion}`,
            "event: customer_state_changed",
            "retry: 3000",
            `data: ${JSON.stringify({ snapshot: current })}`,
            "",
            "",
          ].join("\n"));
        };
        request.signal.addEventListener("abort", close, { once: true });
        send("retry: 3000\n\n");
        sendSnapshot();
        if (terminal.has(current.state.status)) {
          close();
          return;
        }
        void (async () => {
          try {
            while (!closed) {
              await abortableDelay(1500, request.signal);
              if (closed) return;
              current = await loadCustomerAnalysisSnapshot({
                threadId: run.threadId,
                actorId: resolvedActorId,
                runId,
              });
              sendSnapshot();
              if (terminal.has(current.state.status)) {
                close();
                return;
              }
              if (Date.now() - lastHeartbeatAt >= 15000) {
                send(`: heartbeat ${Date.now()}\n\n`);
                lastHeartbeatAt = Date.now();
              }
            }
          } catch (error) {
            if (closed || request.signal.aborted) return;
            await customerJsonError(error, {
              actorId,
              runId,
              threadId: run.threadId,
            });
            closed = true;
            controller.error(error);
          }
        })();
      },
    });
    return new Response(stream, {
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no",
      },
    });
  } catch (error) {
    return customerJsonError(error, { actorId, runId });
  }
}

function abortableDelay(milliseconds: number, signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timeout = setTimeout(done, milliseconds);
    function done() {
      clearTimeout(timeout);
      signal.removeEventListener("abort", done);
      resolve();
    }
    signal.addEventListener("abort", done, { once: true });
  });
}
