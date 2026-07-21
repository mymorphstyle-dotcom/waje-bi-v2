import {
  customerJsonError,
  loadCustomerAnalysisSnapshot,
  requireThread,
} from "../../../_conversationStore";
import { resolveCustomerActor } from "../../../_customerActor";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ threadId: string }> };

export async function GET(request: Request, context: RouteContext) {
  const { threadId } = await context.params;
  let actorId: string | undefined;
  try {
    actorId = resolveCustomerActor(request);
    await requireThread(threadId, actorId);
    const snapshot = await loadCustomerAnalysisSnapshot({ threadId, actorId });
    const lastEventId = request.headers.get("last-event-id")?.trim() ?? "";
    const encoder = new TextEncoder();
    const terminal = new Set(["completed", "completed_with_limits", "failed"]);
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        let closed = false;
        let current = snapshot;
        let deliveredCursor = lastEventId;
        let lastHeartbeatAt = Date.now();
        const close = () => {
          if (closed) return;
          closed = true;
          controller.close();
        };
        const send = (value: string) => controller.enqueue(encoder.encode(value));
        const sendSnapshot = () => {
          const cursor = current.transport.eventCursor;
          if (cursor === deliveredCursor) return;
          deliveredCursor = cursor;
          send([
            `id: ${cursor}`,
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
              current = await loadCustomerAnalysisSnapshot({ threadId, actorId: actorId! });
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
            await customerJsonError(error, { actorId, threadId });
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
    return customerJsonError(error, { actorId, threadId });
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
