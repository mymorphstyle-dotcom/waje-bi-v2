import { NextResponse } from "next/server";

import type { TraceRun } from "../../agent-run-workbench/contracts";
import { listPersistedAnswerPackageRuns, listPersistedRuntimeRuns, type PersistedRuntimeRun } from "../_conversationStore";
import { traceRunFromAnswerPackage } from "../replays/route";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const [persistedAnswerPackageRuns, persistedRuntimeRuns] = await Promise.all([
    listPersistedAnswerPackageRuns(),
    listPersistedRuntimeRuns(),
  ]);
  const persistedRuns = persistedAnswerPackageRuns.map((row) =>
    traceRunFromAnswerPackage(withRunNodes(row.answerPackage, row.runNodes), {
      id: `persisted:${row.runId}`,
      label: `${row.question || row.runId} · 实时运行`,
      question: row.question,
      sourceArtifact: `postgres:${row.runId}`,
      generatedAt: Date.parse(row.createdAt) || 0,
    }),
  );
  const runtimeRuns = persistedRuntimeRuns.map(traceRunFromRuntimeRun);
  return NextResponse.json({
    runs: [...runtimeRuns, ...persistedRuns].sort(
      (left, right) => (right.generatedAt ?? 0) - (left.generatedAt ?? 0),
    ),
  });
}

function withRunNodes(answerPackage: Record<string, unknown>, runNodes: Record<string, unknown>[]) {
  if (!runNodes.length) return answerPackage;
  return {
    ...answerPackage,
    checkpoint_events: runNodes,
  };
}

function traceRunFromRuntimeRun(row: PersistedRuntimeRun): TraceRun {
  const clarification = row.request.clarification as RuntimeClarification | undefined;
  const question = clarification?.questions?.[0];
  const nodes = [
    {
      id: `${row.runId}:status`,
      index: 1,
      node: "run_status",
      label: "接收用户问题",
      owner: "本地系统" as const,
      status: row.runStatus,
      durationMs: 0,
      summary: statusSummary(row.runStatus),
      audit: { status: row.runStatus },
    },
    ...(question
      ? [{
          id: `${row.runId}:question_tool`,
          index: 2,
          node: "question_tool",
          label: "请求业务确认",
          owner: "本地系统" as const,
          status: clarification?.status ?? "waiting_for_user",
          route: "暂停等待用户选择",
          durationMs: 0,
          summary: question.question,
          audit: clarification,
        }]
      : []),
  ];
  return {
    id: `runtime:${row.runId}`,
    label: `${row.question || row.runId} · ${statusLabel(row.runStatus)}`,
    question: row.question,
    status: row.runStatus,
    runId: row.runId,
    generatedAt: Date.parse(row.createdAt) || 0,
    summaryCards: [
      { label: "状态", value: statusLabel(row.runStatus), detail: row.runStatus === "waiting_for_clarification" ? "等待用户确认后继续执行" : "" },
      ...(question ? [{ label: "下一步", value: "回答澄清问题", detail: question.options.map((option) => option.label).join(" / ") }] : []),
    ],
    businessThreads: question ? [{ label: "问题边界", value: "需要确认", detail: clarification?.reason ?? "" }] : [],
    traceClaims: [],
    traceEvidence: [],
    messages: [
      { id: "user-question", role: "user", text: row.question, title: "用户" },
      ...(question
        ? [{
            id: "question-tool",
            role: "assistant" as const,
            nodeId: `${row.runId}:question_tool`,
            title: "需要确认",
            text: `${question.question}\n${question.options.map((option) => `${option.label}${option.recommended ? "（推荐）" : ""}：${option.description}`).join("\n")}`,
          }]
        : []),
    ],
    timing: { actualDurationMs: 0, playbackDurationMs: 0 },
    processSummary: {
      checkpointCount: nodes.length,
      llmCallCount: 0,
      acceptedGraph: [],
      verifierStatus: row.runStatus,
      sourceArtifact: `postgres:${row.runId}`,
      debugStage: row.runStatus,
      nodes,
    },
  };
}

type RuntimeClarification = {
  clarification_id?: string;
  reason?: string;
  status?: string;
  questions?: {
    question: string;
    options: { label: string; description: string; recommended?: boolean }[];
  }[];
};

function statusSummary(status: string) {
  if (status === "waiting_for_clarification") return "当前分析暂停，等待用户确认业务口径或问题绑定。";
  if (status === "failed") return "运行失败，等待查看错误和重跑条件。";
  return "运行已创建，等待后续事件更新。";
}

function statusLabel(status: string) {
  if (status === "waiting_for_clarification") return "等待确认";
  if (status === "completed_without_workflow") return "无需执行";
  if (status === "running_workflow") return "执行中";
  return status;
}
