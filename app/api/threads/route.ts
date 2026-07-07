import { NextRequest, NextResponse } from "next/server";

import { conversationStore, createThread } from "../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  return NextResponse.json({ threads: [...conversationStore().threads.values()] });
}

export async function POST(request: NextRequest) {
  const body = await request.json().catch(() => ({}));
  const thread = createThread(typeof body.ownerId === "string" ? body.ownerId : undefined);
  return NextResponse.json({ thread }, { status: 201 });
}
