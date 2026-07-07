import { NextResponse } from "next/server";

import { GET as getReplays } from "../replays/route";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const response = await getReplays();
  const data = await response.json();
  return NextResponse.json({ runs: data.replays ?? [] });
}
