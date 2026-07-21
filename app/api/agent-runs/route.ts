import { NextResponse } from "next/server";

import type { TraceRun } from "../../agent-run-workbench/contracts";
import {
  listPersistedAgentRunCandidates,
  type PersistedRuntimeRun,
} from "../_conversationStore";
import {
  canonicalizeTraceRuns,
  traceRunFromCustomerPublication,
  traceRunFromRuntimeState,
} from "../_customerRunProjection";
import { assertInternalRouteAvailable } from "../_customerActor";
import { jsonError } from "../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  try {
    assertInternalRouteAvailable();
    const {
      publicationRuns: persistedPublicationRuns,
      runtimeRuns: persistedRuntimeRuns,
    } = await listPersistedAgentRunCandidates();
    const publicationRuns = persistedPublicationRuns.map((row) =>
      traceRunFromCustomerPublication(row.customerPublication, row.publication, {
        id: `run:${row.runId}`,
        label: runLabel(row.question, "已发布", row.createdAt, row.runId),
        question: row.question,
        runId: row.runId,
        runStatus: row.runStatus,
        request: row.request,
        runNodes: row.runNodes,
        workflowTransitions: row.workflowTransitions,
        stageTimings: row.stageTimings,
        evidenceRefs: row.evidenceRefs,
        claimEvidenceLinks: row.claimEvidenceLinks,
        acceptedGraph: row.acceptedGraph,
        verifierStatus: row.verifierStatus,
        humanReview: row.humanReview,
        createdAt: row.createdAt,
        updatedAt: row.updatedAt,
        generatedAt: Date.parse(row.createdAt),
      }),
    );
    const runtimeRuns = persistedRuntimeRuns.map(traceRunFromRuntimeRun);
    return NextResponse.json({
      runs: canonicalizeTraceRuns([...publicationRuns, ...runtimeRuns]),
    });
  } catch (error) {
    return jsonError(error);
  }
}

function traceRunFromRuntimeRun(row: PersistedRuntimeRun): TraceRun {
  return traceRunFromRuntimeState({
    id: `run:${row.runId}`,
    label: runLabel(
      row.question,
      statusLabel(row.runStatus),
      row.createdAt,
      row.runId,
    ),
    runId: row.runId,
    runStatus: row.runStatus,
    question: row.question,
    request: row.request,
    runNodes: row.runNodes,
    workflowTransitions: row.workflowTransitions,
    stageTimings: row.stageTimings,
    evidenceRefs: row.evidenceRefs,
    acceptedGraph: row.acceptedGraph,
    createdAt: row.createdAt,
    updatedAt: row.updatedAt,
    generatedAt: Date.parse(row.createdAt),
  });
}

function runLabel(
  question: string,
  state: string,
  createdAt: string,
  runId: string,
) {
  const timestamp = Number.isNaN(Date.parse(createdAt))
    ? "时间未记录"
    : new Date(createdAt).toISOString().slice(0, 16).replace("T", " ") + "Z";
  return `${question || "未命名分析"} · ${timestamp} · ${shortRunId(runId)} · ${state}`;
}

function shortRunId(runId: string) {
  const normalized = runId.startsWith("run-") ? runId.slice(4) : runId;
  return normalized.slice(0, 8);
}

function statusLabel(status: string) {
  if (status === "waiting_for_clarification") return "等待确认";
  if (status === "interaction_completed") return "交互已完成";
  if (status === "planned") return "计划已确认";
  if (status === "evidence_ready") return "证据已就绪";
  if (status === "authority_sealed") return "权威结论已封存";
  if (status === "narrative_ready") return "业务参考已生成";
  if (status === "running_workflow") return "执行中";
  if (status === "failed") return "运行失败";
  return status;
}
