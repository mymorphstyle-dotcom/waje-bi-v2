import { NextRequest, NextResponse } from "next/server";

import { runAgentCore } from "../../../_agentCore";
import { resolveCustomerActor } from "../../../_customerActor";
import {
  acquireRunDispatchLease,
  claimRunDispatchRequest,
  completeOwnedRunDispatch,
  customerJsonError,
  failOwnedRunDispatch,
  gatewayError,
  loadCustomerAnalysisSnapshot,
  recordCustomerRunStateFromAgentResult,
  runDispatchRequestIdentity,
} from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ threadId: string }> };

type TopicSelection = {
  sourceRunId: string;
  topicId: string;
};

type TopicChoiceAnswer = {
  sourceRunId: string;
  answer: string;
};

export async function POST(request: NextRequest, context: RouteContext) {
  const { threadId } = await context.params;
  const body = await request.json().catch(() => ({})) as Record<string, unknown>;
  const text = typeof body.message === "string" ? body.message : "";
  const message = text.trim();
  if (!message) return customerJsonError(gatewayError("message_required"), { threadId });
  let actorId: string | undefined;
  let ownedDispatch: {
    dispatchId: string;
    runId: string;
    ownerId: string;
    leaseEpoch: number;
  } | null = null;

  try {
    validateMessageBodyShape(body);
    const topicSelection = topicSelectionFrom(body.topicSelection);
    const topicChoiceAnswer = topicChoiceAnswerFrom(body.topicChoiceAnswer);
    if (topicSelection && topicChoiceAnswer) {
      throw gatewayError("topic_choice_input_conflict");
    }
    if (topicChoiceAnswer && topicChoiceAnswer.answer !== text) {
      throw gatewayError("topic_choice_answer_message_mismatch");
    }
    actorId = resolveCustomerActor(request);
    const requestIdentity = runDispatchRequestIdentity(request, body);
    const claim = await claimRunDispatchRequest({
      producerKind: "thread_message",
      scopeRef: threadId,
      requestIdentity,
      threadId,
      text: message,
      actorId,
      requestPayload: {
        message,
        ...(topicSelection ? { topicSelection } : {}),
        ...(topicChoiceAnswer ? { topicChoiceAnswer } : {}),
      },
    });
    const dispatch = await acquireRunDispatchLease({
      dispatchId: claim.dispatch.dispatchId,
      runId: claim.run.id,
    });
    if (!dispatch.acquired || !dispatch.ownerId) {
      return NextResponse.json(
        {
          snapshot: await loadCustomerAnalysisSnapshot({
            threadId,
            actorId,
            runId: claim.run.id,
          }),
        },
        { status: 202 },
      );
    }
    const dispatchOwnerId = dispatch.ownerId;
    ownedDispatch = {
      dispatchId: claim.dispatch.dispatchId,
      runId: claim.run.id,
      ownerId: dispatchOwnerId,
      leaseEpoch: dispatch.leaseEpoch,
    };
    const agentCore = await runAgentCore(
      threadId,
      claim.run.id,
      message,
      actorId,
      {
        runDispatch: {
          dispatchId: claim.dispatch.dispatchId,
          ownerId: dispatchOwnerId,
          leaseEpoch: dispatch.leaseEpoch,
        },
        ...(topicSelection ? { topicSelection } : {}),
        ...(topicChoiceAnswer ? { topicChoiceAnswer } : {}),
        onDetachedWorkerExit: () => failOwnedRunDispatch({
          dispatchId: claim.dispatch.dispatchId,
          runId: claim.run.id,
          ownerId: dispatchOwnerId,
          leaseEpoch: dispatch.leaseEpoch,
          failureReason: "agent_core_worker_exited",
        }).then(() => undefined),
      },
    );
    const agentResult = agentCore?.result && typeof agentCore.result === "object"
      ? agentCore.result as Record<string, unknown>
      : null;
    const runIdMismatch = Boolean(
      agentResult?.run_id && agentResult.run_id !== claim.run.id
    );
    if (agentCore?.error || runIdMismatch) {
      await failOwnedRunDispatch({
        dispatchId: claim.dispatch.dispatchId,
        runId: claim.run.id,
        ownerId: dispatch.ownerId,
        leaseEpoch: dispatch.leaseEpoch,
        failureReason: runIdMismatch
          ? "agent_core_run_id_mismatch"
          : agentCore?.error ?? "agent_core_process_failed",
      });
      ownedDispatch = null;
      throw gatewayError(
        runIdMismatch ? "agent_core_run_id_mismatch" : agentCore?.error ?? "agent_core_process_failed",
      );
    }
    const terminalStatus = agentResult?.status;
    recordCustomerRunStateFromAgentResult(claim.run.id, agentResult);
    if (
      terminalStatus === "completed"
      || terminalStatus === "interaction_completed"
      || terminalStatus === "waiting_for_clarification"
      || terminalStatus === "planned"
      || terminalStatus === "evidence_ready"
      || terminalStatus === "authority_sealed"
      || terminalStatus === "narrative_ready"
      || terminalStatus === "failed"
    ) {
      await completeOwnedRunDispatch({
        dispatchId: claim.dispatch.dispatchId,
        runId: claim.run.id,
        ownerId: dispatch.ownerId,
        leaseEpoch: dispatch.leaseEpoch,
        runStatus: terminalStatus,
      });
      ownedDispatch = null;
    }
    return NextResponse.json(
      {
        snapshot: await loadCustomerAnalysisSnapshot({
          threadId,
          actorId,
          runId: claim.run.id,
        }),
      },
      { status: 202 },
    );
  } catch (error) {
    if (ownedDispatch) {
      const failureReason = error instanceof Error && error.message
        ? error.message
        : "agent_core_process_failed";
      await failOwnedRunDispatch({
        ...ownedDispatch,
        failureReason,
      }).catch(() => undefined);
    }
    return customerJsonError(error, {
      actorId,
      threadId,
      runId: ownedDispatch?.runId,
    });
  }
}

function validateMessageBodyShape(body: Record<string, unknown>) {
  const allowed = new Set([
    "message",
    "requestIdentity",
    "topicSelection",
    "topicChoiceAnswer",
  ]);
  if (Object.keys(body).some((key) => !allowed.has(key))) {
    throw gatewayError("message_request_invalid");
  }
}

function topicSelectionFrom(value: unknown): TopicSelection | undefined {
  if (value === undefined) return undefined;
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw gatewayError("topic_selection_invalid");
  }
  const selection = value as Record<string, unknown>;
  const keys = Object.keys(selection);
  if (
    keys.length !== 2
    || !keys.every((key) => key === "sourceRunId" || key === "topicId")
    || typeof selection.sourceRunId !== "string"
    || selection.sourceRunId.trim().length === 0
    || selection.sourceRunId !== selection.sourceRunId.trim()
    || typeof selection.topicId !== "string"
    || selection.topicId.trim().length === 0
    || selection.topicId !== selection.topicId.trim()
  ) {
    throw gatewayError("topic_selection_invalid");
  }
  return {
    sourceRunId: selection.sourceRunId,
    topicId: selection.topicId,
  };
}

function topicChoiceAnswerFrom(value: unknown): TopicChoiceAnswer | undefined {
  if (value === undefined) return undefined;
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw gatewayError("topic_choice_answer_invalid");
  }
  const choiceAnswer = value as Record<string, unknown>;
  const keys = Object.keys(choiceAnswer);
  if (
    keys.length !== 2
    || !keys.every((key) => key === "sourceRunId" || key === "answer")
    || typeof choiceAnswer.sourceRunId !== "string"
    || choiceAnswer.sourceRunId.trim().length === 0
    || choiceAnswer.sourceRunId !== choiceAnswer.sourceRunId.trim()
    || typeof choiceAnswer.answer !== "string"
    || choiceAnswer.answer.trim().length === 0
    || choiceAnswer.answer !== choiceAnswer.answer.trim()
  ) {
    throw gatewayError("topic_choice_answer_invalid");
  }
  return {
    sourceRunId: choiceAnswer.sourceRunId,
    answer: choiceAnswer.answer,
  };
}
