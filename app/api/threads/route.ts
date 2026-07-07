import { NextRequest, NextResponse } from "next/server";

import { createThread, listThreads } from "../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  return NextResponse.json({ threads: await listThreads() });
}

export async function POST(request: NextRequest) {
  const body = await request.json().catch(() => ({}));
  const thread = await createThread(typeof body.ownerId === "string" ? body.ownerId : undefined);
  return NextResponse.json({ thread }, { status: 201 });
}
