import { jsonError, launchDashboard } from "../../_conversationStore";
import { assertInternalRouteAvailable } from "../../_customerActor";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: Request) {
  const params = new URL(request.url).searchParams;
  const slowMs = Number(params.get("slowMs") ?? 30000);
  const limit = Number(params.get("limit") ?? 20);
  try {
    assertInternalRouteAvailable();
    return Response.json(await launchDashboard({ limit, slowMs }));
  } catch (error) {
    return jsonError(error);
  }
}
