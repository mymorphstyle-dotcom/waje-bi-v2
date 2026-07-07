import { NextResponse } from "next/server";

import { listPersistedAnswerPackageRuns } from "../_conversationStore";
import { GET as getReplays, traceRunFromAnswerPackage } from "../replays/route";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const [response, persistedAnswerPackageRuns] = await Promise.all([
    getReplays(),
    listPersistedAnswerPackageRuns(),
  ]);
  const data = await response.json();
  const persistedRuns = persistedAnswerPackageRuns.map((row) =>
    traceRunFromAnswerPackage(row.answerPackage, {
      id: `persisted:${row.runId}`,
      label: `${row.question || row.runId} · 实时运行`,
      question: row.question,
      sourceArtifact: `postgres:${row.runId}`,
      generatedAt: Date.parse(row.createdAt) || 0,
    }),
  );
  return NextResponse.json({
    runs: [...persistedRuns, ...(data.replays ?? [])].sort(
      (left, right) => (right.generatedAt ?? 0) - (left.generatedAt ?? 0),
    ),
  });
}
