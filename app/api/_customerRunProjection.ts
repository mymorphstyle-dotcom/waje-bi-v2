import type {
  TraceAcceptedTask,
  TraceAcceptedTaskExecution,
  TraceCapabilityFailure,
  TraceCapabilityOutcomeStatus,
  TraceCapabilityRetryability,
  TraceClaim,
  TraceCompleteness,
  TraceEvidence,
  TraceEvidenceBindingState,
  TraceEvidenceExecutionState,
  TraceEvidencePlanState,
  TraceHumanReview,
  TraceLifecycleOutcome,
  TraceLifecycleState,
  TraceNode,
  TraceNodeOutcome,
  TraceOwner,
  TraceRun,
  TraceRunOutcome,
} from "../agent-run-workbench/contracts";

import type { SafePublicationRefs } from "./_conversationStore";
import {
  parseCustomerPublication,
  type CustomerPublication,
} from "./_customerPublicationContract";

type JsonObject = Record<string, unknown>;

type ProjectionRows = {
  runNodes?: JsonObject[];
  workflowTransitions?: JsonObject[];
  stageTimings?: JsonObject[];
  evidenceRefs?: JsonObject[];
  claimEvidenceLinks?: JsonObject[];
  acceptedGraph?: JsonObject[];
  verifierStatus?: JsonObject;
  humanReview?: JsonObject;
  request?: JsonObject;
  createdAt?: string;
  updatedAt?: string;
};

export type RuntimeTraceProjectionInput = ProjectionRows & {
  id: string;
  label: string;
  runId: string;
  runStatus: string;
  question: string;
  generatedAt?: number;
};

const LLM_TRANSITION_NODES = new Set([
  "conversation_entry",
  "bind_intent",
  "generate_clarification",
  "compile_authoritative_plan",
  "compile_plan_patch",
]);

const DURABLE_TIMING_REQUIRED_NODES = new Set([
  ...LLM_TRANSITION_NODES,
  "execute_capability_dag",
  "settle_claim_authority",
  "compose_claim_aware_narrative",
]);

const MIXED_TRANSITION_NODES = new Set([
  "settle_claim_authority",
  "compose_claim_aware_narrative",
]);

const LOCAL_SYSTEM_TRANSITION_NODES = new Set([
  "persist_waiting_for_decision",
  "accept_material_decision",
]);

const EVIDENCE_EXECUTION_STATES = new Set<TraceEvidenceExecutionState>([
  "available",
  "unavailable",
  "integrity_failed",
  "technical_failed",
]);

const EVIDENCE_PLAN_STATES = new Set<TraceEvidencePlanState>([
  "active",
  "superseded",
]);

const EVIDENCE_BINDING_STATES = new Set<TraceEvidenceBindingState>([
  "bound",
  "unsettled",
]);

const CAPABILITY_OUTCOME_STATUSES = new Set<TraceCapabilityOutcomeStatus>([
  "succeeded",
  "unavailable",
  "integrity_failed",
  "technical_failed",
  "skipped",
  "superseded",
]);

const CAPABILITY_RETRYABILITY_STATES = new Set<TraceCapabilityRetryability>([
  "never",
  "same_input",
  "replan_required",
]);

const CAPABILITY_FAILURE_LAYERS = new Set<TraceCapabilityFailure["layer"]>([
  "query",
  "capability",
  "evidence",
  "persistence",
]);

const CAPABILITY_FAILURE_INTEGRITY_LEVELS = new Set<
  TraceCapabilityFailure["integrityLevel"]
>([
  "expected_boundary",
  "task",
  "shared_authority",
]);

export function traceRunFromCustomerPublication(
  customerPublication: JsonObject,
  publication: SafePublicationRefs,
  options: ProjectionRows & {
    id: string;
    label: string;
    runId: string;
    runStatus?: string;
    question?: string;
    generatedAt?: number;
  },
): TraceRun {
  const customer = validateCustomerPublication(customerPublication);
  validatePublicationRefs(publication);
  const publishedBlocks = customer.blocks;
  const answerText = publishedBlocks.map((block) => block.text).join("\n\n");
  const limitations = customer.limitation_refs;
  const trace = appendPublicationNodes(
    projectTraceRows({
      ...options,
      claimEvidenceLinks: options.claimEvidenceLinks?.filter((link) =>
        customer.claim_refs.includes(requiredClaimRef(link))
      ),
    }),
    options.runId,
    publication,
    customer.claim_refs,
  );
  if (trace.acceptedGraph?.some((task) => task.execution.state !== "settled")) {
    throw new Error("workbench_publication_accepted_graph_unsettled");
  }
  const claims = projectClaims(customer, options.claimEvidenceLinks);
  const evidence = projectEvidence(options.evidenceRefs);
  const lifecycle = publicationLifecycle(
    options.runStatus ?? "completed",
    options.request,
    publication,
    options.verifierStatus,
    publishedBlocks.length,
  );
  const runOutcome = settleRunOutcome(lifecycle, options.runStatus ?? "completed");
  const humanReview = projectHumanReview(options.humanReview, true);
  const summaryCards = [
    { label: "运行结果", value: runOutcomeLabel(runOutcome) },
    { label: "已发布回答", value: String(publishedBlocks.length) },
    { label: "已发布结论", value: String(customer.claim_refs.length) },
    { label: "已声明边界", value: String(limitations.length) },
  ];

  return {
    id: options.id,
    label: options.label,
    question: options.question ?? "",
    status: options.runStatus ?? "completed",
    runOutcome,
    runMode: trace.runMode,
    runId: options.runId,
    generatedAt: options.generatedAt,
    summaryCards,
    businessThreads: [
      {
        label: "权威包",
        value: publication.authority_bundle_ref,
        detail: publication.authority_bundle_digest,
      },
      {
        label: "发布版本",
        value: publication.publication_ref,
        detail: publication.publication_digest,
      },
      {
        label: "客户投影",
        value: publication.projection_id,
        detail: publication.projection_digest,
      },
    ],
    traceClaims: claims,
    traceEvidence: evidence,
    messages: [
      ...(options.question
        ? [{ id: "user-question", role: "user" as const, text: options.question, title: "用户" }]
        : []),
      {
        id: "customer-publication",
        role: "assistant" as const,
        title: "权威分析结果",
        text: answerText,
        nodeId: trace.nodes.at(-1)?.id,
      },
    ],
    answer: {
      status: lifecycle.publication.status ?? "unknown",
      answerText,
      claims,
      limitations,
      evidence,
    },
    humanReview,
    lifecycle,
    traceCompleteness: traceCompleteness({
      trace,
      claimsPresent: options.claimEvidenceLinks !== undefined,
      evidenceCompleteness: evidenceTraceCompleteness(options.evidenceRefs),
    }),
    timing: trace.timing,
    processSummary: {
      checkpointCount: trace.nodes.length,
      llmCallCount: trace.llmCallCount,
      acceptedGraph: trace.acceptedGraph,
      verifierStatus: lifecycle.verifier.status,
      nodes: trace.nodes,
    },
  };
}

export function traceRunFromRuntimeState(
  input: RuntimeTraceProjectionInput,
): TraceRun {
  const trace = projectTraceRows(input);
  const lifecycle = runtimeLifecycle(
    input.runStatus,
    input.request,
    input.verifierStatus,
  );
  const runOutcome = settleRunOutcome(lifecycle, input.runStatus);
  const claims = projectClaims(undefined, input.claimEvidenceLinks);
  const evidence = projectEvidence(input.evidenceRefs);

  return {
    id: input.id,
    label: input.label,
    question: input.question,
    status: input.runStatus,
    runOutcome,
    runMode: trace.runMode,
    runId: input.runId,
    generatedAt: input.generatedAt,
    summaryCards: lifecycleCards(lifecycle),
    businessThreads: runtimeBusinessThreads(input.request),
    traceClaims: claims,
    traceEvidence: evidence,
    messages: runtimeMessages(input, trace.nodes),
    humanReview: projectHumanReview(input.humanReview, false),
    lifecycle,
    traceCompleteness: traceCompleteness({
      trace,
      claimsPresent: input.claimEvidenceLinks !== undefined,
      evidenceCompleteness: evidenceTraceCompleteness(input.evidenceRefs),
    }),
    timing: trace.timing,
    processSummary: {
      checkpointCount: trace.nodes.length,
      llmCallCount: trace.llmCallCount,
      acceptedGraph: trace.acceptedGraph,
      verifierStatus: lifecycle.verifier.status,
      nodes: trace.nodes,
    },
  };
}

export function canonicalizeTraceRuns(runs: TraceRun[]): TraceRun[] {
  const canonical = new Map<string, TraceRun>();
  for (const run of runs) {
    const current = canonical.get(run.runId);
    if (
      !current
      || (Boolean(run.answer) && !current.answer)
      || (Boolean(run.answer) === Boolean(current.answer)
        && canonicalScore(run) > canonicalScore(current))
    ) {
      canonical.set(run.runId, run);
    }
  }
  return [...canonical.values()].sort(
    (left, right) => (right.generatedAt ?? 0) - (left.generatedAt ?? 0),
  );
}

function projectTraceRows(rows: ProjectionRows) {
  const acceptedTransitions = rows.workflowTransitions === undefined
    ? undefined
    : rows.workflowTransitions.filter(isAcceptedTransition);
  const stageTimingByAttempt = projectStageTimings(rows.stageTimings);
  validateEvidenceExecutionBindings(rows.evidenceRefs, acceptedTransitions);
  const claimRefs = rows.claimEvidenceLinks?.map(requiredClaimRef);
  const nodes = acceptedTransitions !== undefined && acceptedTransitions.length > 0
    ? acceptedTransitions.map((transition, index) => {
        const transitionAttemptId = requiredString(
          transition.attempt_id,
          "workbench_transition_invalid",
        );
        const stageTiming = stageTimingByAttempt?.get(transitionAttemptId);
        if (
          stageTiming
          && requiredString(
            stageTiming.stage_name,
            "workbench_stage_timing_invalid",
          ) !== requiredString(
            transition.node_name,
            "workbench_transition_invalid",
          )
        ) throw new Error("workbench_stage_timing_transition_mismatch");
        return transitionNode({
          transition,
          stageTiming,
          index,
          evidenceRows: rows.evidenceRefs,
          claimRefs,
        });
      })
    : snapshotNodes(rows.runNodes, rows.evidenceRefs, claimRefs);
  if (stageTimingByAttempt && acceptedTransitions !== undefined) {
    const acceptedAttemptIds = new Set(
      acceptedTransitions.map((transition) => requiredString(
        transition.attempt_id,
        "workbench_transition_invalid",
      )),
    );
    if ([...stageTimingByAttempt.keys()].some((attemptId) => !acceptedAttemptIds.has(attemptId))) {
      throw new Error("workbench_stage_timing_transition_missing");
    }
  }
  const acceptedGraph = projectAcceptedGraph(rows.acceptedGraph);
  const llmCallCount = rows.stageTimings === undefined
    ? undefined
    : rows.stageTimings.reduce(
        (count, timing) => count + requiredNonNegativeInteger(
          timing.llm_call_count,
          "workbench_stage_timing_invalid",
        ),
        0,
      );
  const requiredTimingAttemptIds = acceptedTransitions?.flatMap((transition) =>
    transitionRequiresDurableTiming(transition)
      ? [requiredString(transition.attempt_id, "workbench_transition_invalid")]
      : []
  );
  const llmCompleteness: TraceCompleteness = rows.stageTimings === undefined
    ? "unknown"
    : requiredTimingAttemptIds === undefined
      ? "incomplete"
      : requiredTimingAttemptIds.every((attemptId) => stageTimingByAttempt?.has(attemptId))
        ? "known"
        : "incomplete";
  const replayDurationMs = chronologyDurationMs(nodes);
  const actualDurationMs = elapsedMs(rows.createdAt, rows.updatedAt)
    ?? replayDurationMs;
  const timingCompleteness: TraceCompleteness = actualDurationMs === undefined
    ? "unknown"
    : "known";

  return {
    nodes,
    acceptedGraph,
    llmCallCount,
    runMode: acceptedTransitions !== undefined
      && acceptedTransitions.length > 0
      && hasCompleteNodeChronology(nodes)
      && replayDurationMs !== undefined
      ? "event_replay" as const
      : "static_snapshot" as const,
    chronologyCompleteness: acceptedTransitions === undefined
      ? rows.runNodes?.length
        ? "incomplete" as const
        : "unknown" as const
      : acceptedTransitions.length === 0
        ? rows.runNodes?.length
          ? "incomplete" as const
          : "known" as const
        : hasCompleteNodeChronology(nodes)
          ? "known" as const
          : "incomplete" as const,
    llmCompleteness,
    graphCompleteness: acceptedGraph === undefined
      ? "unknown" as const
      : acceptedGraph.some((task) => task.execution.state === "unsettled")
        ? "incomplete" as const
        : "known" as const,
    timing: {
      ...(actualDurationMs === undefined ? {} : { actualDurationMs }),
      completeness: timingCompleteness,
    },
  };
}

function transitionNode({
  transition,
  stageTiming,
  index,
  evidenceRows,
  claimRefs,
}: {
  transition: JsonObject;
  stageTiming?: JsonObject;
  index: number;
  evidenceRows?: JsonObject[];
  claimRefs?: string[];
}): TraceNode {
  const nodeName = requiredString(transition.node_name, "workbench_transition_invalid");
  const transitionAttemptId = requiredString(
    transition.attempt_id,
    "workbench_transition_invalid",
  );
  const status = requiredString(transition.status, "workbench_transition_invalid");
  const providerRef = optionalString(transition.provider_ref);
  const durableTimingRequired = transitionRequiresDurableTiming(transition);
  const startedAt = stageTiming
    ? requiredTimestamp(stageTiming.started_at, "workbench_stage_timing_invalid")
    : durableTimingRequired
      ? undefined
      : optionalTimestamp(transition.started_at);
  const finishedAt = stageTiming
    ? requiredTimestamp(stageTiming.finished_at, "workbench_stage_timing_invalid")
    : durableTimingRequired
      ? undefined
      : optionalTimestamp(transition.finished_at);
  const durationMs = elapsedMs(startedAt, finishedAt);
  const projection = runtimeNodeProjection(nodeName);
  const route = optionalString(transition.next_transition);
  const executionEvidence = nodeName === "execute_capability_dag"
    ? evidenceForExecutionTransition(transition, evidenceRows)
    : undefined;
  const nodeClaimRefs = ["settle_claim_authority", "compose_claim_aware_narrative"].includes(nodeName)
    ? claimRefs
    : undefined;

  return {
    id: transitionAttemptId,
    index: index + 1,
    node: nodeName,
    label: projection.label,
    owner: transitionOwner(nodeName, providerRef, stageTiming),
    status,
    outcome: nodeOutcome(status),
    ...(route ? { route } : {}),
    ...(durationMs === undefined ? {} : { durationMs }),
    ...(startedAt ? { startedAt } : {}),
    ...(finishedAt ? { finishedAt } : {}),
    ...(executionEvidence?.evidenceRefs === undefined
      ? {}
      : { evidenceRefs: executionEvidence.evidenceRefs }),
    ...(executionEvidence === undefined
      ? {}
      : { evidenceCompleteness: executionEvidence.completeness }),
    ...(nodeClaimRefs === undefined ? {} : { claimRefs: nodeClaimRefs }),
    summary: projection.summary,
  };
}

function projectStageTimings(rows: JsonObject[] | undefined) {
  if (rows === undefined) return undefined;
  const timings = new Map<string, JsonObject>();
  for (const row of rows) {
    const transitionAttemptId = requiredString(
      row.transition_attempt_id,
      "workbench_stage_timing_invalid",
    );
    const stageName = requiredString(row.stage_name, "workbench_stage_timing_invalid");
    const startedAt = requiredTimestamp(row.started_at, "workbench_stage_timing_invalid");
    const finishedAt = requiredTimestamp(row.finished_at, "workbench_stage_timing_invalid");
    const acceptedCallCount = requiredPositiveInteger(
      row.accepted_call_count,
      "workbench_stage_timing_invalid",
    );
    const llmCallCount = requiredNonNegativeInteger(
      row.llm_call_count,
      "workbench_stage_timing_invalid",
    );
    const controlCallCount = requiredNonNegativeInteger(
      row.control_call_count,
      "workbench_stage_timing_invalid",
    );
    const queryCallCount = requiredNonNegativeInteger(
      row.query_call_count,
      "workbench_stage_timing_invalid",
    );
    const capabilityCallCount = requiredNonNegativeInteger(
      row.capability_call_count,
      "workbench_stage_timing_invalid",
    );
    if (
      timestampMs(finishedAt)! < timestampMs(startedAt)!
      || timings.has(transitionAttemptId)
      || llmCallCount + controlCallCount + queryCallCount + capabilityCallCount
        !== acceptedCallCount
    ) {
      throw new Error("workbench_stage_timing_invalid");
    }
    timings.set(transitionAttemptId, row);
  }
  return timings;
}

function projectAcceptedGraph(rows: JsonObject[] | undefined): TraceAcceptedTask[] | undefined {
  if (rows === undefined) return undefined;
  const taskIds = new Set<string>();
  const outcomeRefs = new Set<string>();
  const planRevisionIds = new Set<string>();
  const tasks = rows.map((row) => {
    const taskId = requiredString(row.task_id, "workbench_accepted_graph_invalid");
    const planRevisionId = requiredString(
      row.plan_revision_id,
      "workbench_accepted_graph_invalid",
    );
    const execution = projectAcceptedTaskExecution(row);
    if (taskIds.has(taskId)) {
      throw new Error("workbench_accepted_graph_invalid");
    }
    taskIds.add(taskId);
    if (execution.state === "settled") {
      if (outcomeRefs.has(execution.outcomeRef)) {
        throw new Error("workbench_accepted_graph_invalid");
      }
      outcomeRefs.add(execution.outcomeRef);
    }
    planRevisionIds.add(planRevisionId);
    return {
      taskId,
      planRevisionId,
      capabilityId: requiredString(
        row.capability_id,
        "workbench_accepted_graph_invalid",
      ),
      taskKey: requiredString(row.task_key, "workbench_accepted_graph_invalid"),
      execution,
    };
  });
  if (planRevisionIds.size > 1) throw new Error("workbench_accepted_graph_invalid");
  return tasks;
}

function projectAcceptedTaskExecution(row: JsonObject): TraceAcceptedTaskExecution {
  const state = requiredString(
    row.execution_state,
    "workbench_accepted_graph_invalid",
  );
  if (state === "not_started" || state === "unsettled") {
    if ([
      row.outcome_ref,
      row.status,
      row.retryability,
      row.limitation_refs,
      row.failure,
    ].some((value) => value !== null && value !== undefined)) {
      throw new Error("workbench_accepted_graph_invalid");
    }
    return { state };
  }
  if (state !== "settled") throw new Error("workbench_accepted_graph_invalid");
  const outcomeRef = requiredString(
    row.outcome_ref,
    "workbench_accepted_graph_invalid",
  );
  const status = requiredCapabilityOutcomeStatus(row.status);
  const retryability = requiredCapabilityRetryability(row.retryability);
  const limitationRefs = requiredUniqueStringArray(
    row.limitation_refs,
    "workbench_accepted_graph_invalid",
  );
  const failure = projectCapabilityFailure(row.failure);
  if (
    (status === "succeeded" && failure !== undefined)
    || (
      (status === "integrity_failed" || status === "technical_failed")
      && failure === undefined
    )
  ) {
    throw new Error("workbench_accepted_graph_invalid");
  }
  return {
    state,
    outcomeRef,
    status,
    retryability,
    limitationRefs,
    ...(failure === undefined ? {} : { failure }),
  };
}

function requiredCapabilityOutcomeStatus(value: unknown) {
  if (!CAPABILITY_OUTCOME_STATUSES.has(value as TraceCapabilityOutcomeStatus)) {
    throw new Error("workbench_accepted_graph_invalid");
  }
  return value as TraceCapabilityOutcomeStatus;
}

function requiredCapabilityRetryability(value: unknown) {
  if (!CAPABILITY_RETRYABILITY_STATES.has(value as TraceCapabilityRetryability)) {
    throw new Error("workbench_accepted_graph_invalid");
  }
  return value as TraceCapabilityRetryability;
}

function projectCapabilityFailure(value: unknown): TraceCapabilityFailure | undefined {
  if (value === null || value === undefined) return undefined;
  if (
    !isJsonObject(value)
    || !sameStringSet(Object.keys(value), [
      "layer",
      "kind",
      "integrity_level",
      "business_boundary",
    ])
    || !CAPABILITY_FAILURE_LAYERS.has(
      value.layer as TraceCapabilityFailure["layer"],
    )
    || !CAPABILITY_FAILURE_INTEGRITY_LEVELS.has(
      value.integrity_level as TraceCapabilityFailure["integrityLevel"],
    )
  ) {
    throw new Error("workbench_accepted_graph_invalid");
  }
  return {
    layer: value.layer as TraceCapabilityFailure["layer"],
    kind: requiredString(value.kind, "workbench_accepted_graph_invalid"),
    integrityLevel: value.integrity_level as TraceCapabilityFailure["integrityLevel"],
    businessBoundary: requiredString(
      value.business_boundary,
      "workbench_accepted_graph_invalid",
    ),
  };
}

function evidenceForExecutionTransition(
  transition: JsonObject,
  evidenceRows: JsonObject[] | undefined,
) {
  if (evidenceRows === undefined) {
    return { completeness: "unknown" as const };
  }
  const transitionAttemptId = requiredString(
    transition.attempt_id,
    "workbench_transition_invalid",
  );
  const executionSnapshotRef = optionalString(transition.execution_snapshot_ref);
  const executionPlanRevisionId = optionalString(transition.execution_plan_revision_id);
  const expectedEntryRefs = Array.isArray(transition.execution_evidence_entry_refs)
    ? requiredStringArray(
        transition.execution_evidence_entry_refs,
        "workbench_execution_evidence_binding_invalid",
      )
    : undefined;
  if (!executionSnapshotRef || !executionPlanRevisionId || expectedEntryRefs === undefined) {
    return { completeness: "incomplete" as const };
  }
  const boundRows = evidenceRows.filter((row) =>
    row.binding_state === "bound"
    && row.execution_transition_attempt_id === transitionAttemptId
  );
  const actualEntryRefs = boundRows.map((row) => requiredString(
    row.entry_ref,
    "workbench_execution_evidence_binding_invalid",
  ));
  const closureKnown = sameStringSet(actualEntryRefs, expectedEntryRefs)
    && boundRows.every((row) => row.plan_revision_id === executionPlanRevisionId);
  return {
    evidenceRefs: boundRows.map(requiredEvidenceRef),
    completeness: closureKnown ? "known" as const : "incomplete" as const,
  };
}

function validateEvidenceExecutionBindings(
  rows: JsonObject[] | undefined,
  acceptedTransitions: JsonObject[] | undefined,
) {
  if (rows === undefined) return;
  const acceptedExecuteAttemptIds = new Set(
    (acceptedTransitions ?? []).flatMap((transition) =>
      transition.node_name === "execute_capability_dag"
        ? [requiredString(transition.attempt_id, "workbench_transition_invalid")]
        : []
    ),
  );
  const evidenceRefs = new Set<string>();
  for (const row of rows) {
    const evidenceRef = requiredEvidenceRef(row);
    if (evidenceRefs.has(evidenceRef)) {
      throw new Error("workbench_evidence_projection_invalid");
    }
    evidenceRefs.add(evidenceRef);
    const bindingState = requiredEvidenceBindingState(row.binding_state);
    const transitionAttemptId = optionalString(row.execution_transition_attempt_id);
    if (
      (bindingState === "bound"
        && (!transitionAttemptId || !acceptedExecuteAttemptIds.has(transitionAttemptId)))
      || (bindingState === "unsettled" && transitionAttemptId !== undefined)
    ) throw new Error("workbench_execution_evidence_binding_invalid");
  }
}

function snapshotNodes(
  persistedNodes: JsonObject[] | undefined,
  evidenceRows?: JsonObject[],
  claimRefs?: string[],
): TraceNode[] {
  if (persistedNodes === undefined) return [];
  return persistedNodes.map((node, index) => {
    const nodeName = requiredString(
      node.node_name ?? node.node,
      "workbench_snapshot_node_invalid",
    );
    const status = requiredString(node.status, "workbench_snapshot_node_invalid");
    const projection = runtimeNodeProjection(nodeName);
    const startedAt = optionalTimestamp(node.started_at ?? node.startedAt);
    const finishedAt = optionalTimestamp(node.finished_at ?? node.finishedAt);
    const durationMs = elapsedMs(startedAt, finishedAt);
    return {
      id: `snapshot:${index + 1}:${nodeName}`,
      index: index + 1,
      node: nodeName,
      label: projection.label,
      owner: "未知" as const,
      status,
      outcome: nodeOutcome(status),
      ...(durationMs === undefined ? {} : { durationMs }),
      ...(startedAt ? { startedAt } : {}),
      ...(finishedAt ? { finishedAt } : {}),
      ...(nodeName === "execute_capability_dag"
        ? {
            evidenceCompleteness: evidenceRows === undefined
              ? "unknown" as const
              : "incomplete" as const,
          }
        : {}),
      ...(["settle_claim_authority", "compose_claim_aware_narrative"].includes(nodeName)
        && claimRefs !== undefined
        ? { claimRefs }
        : {}),
      summary: `${projection.summary} 当前仅保存业务快照，节点执行主体未记录。`,
    };
  });
}

function appendPublicationNodes(
  trace: ReturnType<typeof projectTraceRows>,
  runId: string,
  publication: SafePublicationRefs,
  claimRefs: string[],
) {
  const publicationNodes: TraceNode[] = [
    {
      id: `${runId}:authority-sealed`,
      index: trace.nodes.length + 1,
      node: "seal_authority_bundle",
      label: "权威结论已封存",
      owner: "本地系统",
      status: "sealed",
      outcome: "completed",
      startedAt: publication.authority_sealed_at,
      finishedAt: publication.authority_sealed_at,
      claimRefs,
      summary: "权威结论与 verifier 结果已封存。",
    },
    {
      id: `${runId}:publication-created`,
      index: trace.nodes.length + 2,
      node: "publish_customer_projection",
      label: "客户投影已发布",
      owner: "本地系统",
      status: "published",
      outcome: "completed",
      startedAt: publication.published_at,
      finishedAt: publication.published_at,
      claimRefs,
      summary: "唯一客户安全投影已生成并绑定到发布版本。",
    },
    {
      id: `${runId}:delivery`,
      index: trace.nodes.length + 3,
      node: "deliver_publication",
      label: publication.delivery_status === "published"
        ? "客户发布已交付"
        : publication.delivery_status === "pending"
          ? "客户发布等待交付"
          : "客户发布交付失败",
      owner: "本地系统",
      status: publication.delivery_status,
      outcome: nodeOutcome(publication.delivery_status),
      ...(publication.delivery_attempted_at
        ? {
            startedAt: publication.delivery_attempted_at,
            finishedAt: publication.delivery_attempted_at,
          }
        : {}),
      summary: publication.delivery_status === "pending"
        ? "客户发布已进入交付队列，尚无交付尝试。"
        : "客户发布的交付结果已持久化。",
    },
  ];
  const nodes: TraceNode[] = [];
  let authorityInserted = false;
  let publicationInserted = false;
  for (const node of trace.nodes) {
    nodes.push(node);
    if (node.node === "settle_claim_authority") {
      nodes.push(publicationNodes[0]);
      authorityInserted = true;
    }
    if (node.node === "compose_claim_aware_narrative") {
      nodes.push(publicationNodes[1], publicationNodes[2]);
      publicationInserted = true;
    }
  }
  if (!authorityInserted || !publicationInserted) {
    throw new Error("workbench_publication_stage_missing");
  }
  const indexedNodes = nodes.map((node, index) => ({ ...node, index: index + 1 }));
  const chronologyComplete = hasCompleteNodeChronology(indexedNodes);
  const replayDurationMs = chronologyDurationMs(indexedNodes);
  return {
    ...trace,
    nodes: indexedNodes,
    runMode: chronologyComplete && replayDurationMs !== undefined
      ? "event_replay" as const
      : "static_snapshot" as const,
    chronologyCompleteness: chronologyComplete ? "known" as const : "incomplete" as const,
  };
}

function publicationLifecycle(
  runStatus: string,
  request: JsonObject | undefined,
  publication: SafePublicationRefs,
  verifierStatus: JsonObject | undefined,
  publishedBlockCount: number,
) {
  const postExecutionStatus = requestStatus(request, "post_execution_status");
  const requestedPublicationStatus = requestStatus(request, "publication_status");
  const requestedDeliveryStatus = requestStatus(request, "delivery_status");
  const expectedRequestState = {
    pending: {
      run: "narrative_ready",
      postExecution: "narrative_ready",
      publication: "ready",
      delivery: "persisted",
    },
    published: {
      run: "completed",
      postExecution: "completed",
      publication: "published",
      delivery: "published",
    },
    retryable_failed: {
      run: "completed",
      postExecution: "delivery_retryable_failed",
      publication: "ready",
      delivery: "retryable_failed",
    },
    permanently_failed: {
      run: "completed",
      postExecution: "delivery_permanently_failed",
      publication: "ready",
      delivery: "permanently_failed",
    },
  }[publication.delivery_status];
  if (
    !expectedRequestState
    || runStatus !== expectedRequestState.run
    || postExecutionStatus !== expectedRequestState.postExecution
    || requestedPublicationStatus !== expectedRequestState.publication
    || requestedDeliveryStatus !== expectedRequestState.delivery
  ) {
    throw new Error("workbench_publication_status_mismatch");
  }
  const lifecycle = runtimeLifecycle(runStatus, request, verifierStatus);
  return {
    ...lifecycle,
    verifier: verifierLifecycle(verifierStatus, publishedBlockCount),
    publication: lifecycleState(
      "complete",
      "published",
      undefined,
      publication.published_at,
    ),
    delivery: lifecycleState(
      publication.delivery_status === "published"
        ? "complete"
        : publication.delivery_status === "pending"
          ? "pending"
          : "failed",
      publication.delivery_status,
      undefined,
      publication.delivery_attempted_at,
    ),
  };
}

function runtimeLifecycle(
  runStatus: string,
  request: JsonObject | undefined,
  verifierStatus: JsonObject | undefined,
) {
  const analysisStatus = requestStatus(request, "analysis_status");
  const publicationStatus = requestStatus(request, "publication_status");
  const deliveryStatus = requestStatus(request, "delivery_status");
  return {
    execution: executionLifecycle(runStatus, analysisStatus),
    verifier: verifierLifecycle(verifierStatus),
    publication: publicationStatus
      ? lifecycleState(lifecycleOutcome(publicationStatus), publicationStatus)
      : unknownLifecycle(),
    delivery: deliveryStatus
      ? lifecycleState(lifecycleOutcome(deliveryStatus), deliveryStatus)
      : unknownLifecycle(),
  };
}

function executionLifecycle(runStatus: string, analysisStatus: string | undefined) {
  if (runStatus === "failed") return lifecycleState("failed", runStatus);
  if (["planned", "evidence_ready", "authority_sealed", "narrative_ready"].includes(runStatus)) {
    return lifecycleState("checkpoint", runStatus);
  }
  if (runStatus === "interaction_completed") {
    return lifecycleState("not_applicable", runStatus, "本轮为 typed interaction，未进入分析执行。");
  }
  if (["queued", "running_workflow"].includes(runStatus)) {
    return lifecycleState("running", runStatus);
  }
  if (runStatus === "waiting_for_clarification") {
    return lifecycleState("pending", runStatus);
  }
  const status = analysisStatus ?? runStatus;
  if (status === "failed") return lifecycleState("failed", status);
  if (["complete", "completed", "boundary_only"].includes(status)) {
    return lifecycleState("complete", status);
  }
  return {
    outcome: "unknown" as const,
    status,
    completeness: "incomplete" as const,
    detail: "运行状态已记录，尚未映射到终态。",
  };
}

function verifierLifecycle(
  value: JsonObject | undefined,
  publishedBlockCount?: number,
): TraceLifecycleState {
  if (value === undefined) return unknownLifecycle();
  const accepted = optionalCount(value.acceptedClaimCount ?? value.accepted_claim_count);
  const vetoed = optionalCount(value.vetoedClaimCount ?? value.vetoed_claim_count);
  const acceptedBlocks = optionalCount(
    value.acceptedBlockCount ?? value.accepted_block_count,
  );
  const rejectedBlocks = optionalCount(
    value.rejectedBlockCount ?? value.rejected_block_count,
  );
  const vetoedBlocks = optionalCount(value.vetoedBlockCount ?? value.vetoed_block_count);
  const claimReports = stringArray(value.claimReportRefs ?? value.claim_report_refs);
  const blockReports = stringArray(value.blockReportRefs ?? value.block_report_refs);
  const verifiedAt = optionalTimestamp(value.verifiedAt ?? value.verified_at);
  const reportCount = claimReports.length + blockReports.length;
  if (publishedBlockCount !== undefined) {
    const missing = [
      accepted === undefined ? "acceptedClaimCount" : null,
      vetoed === undefined ? "vetoedClaimCount" : null,
      acceptedBlocks === undefined ? "acceptedBlockCount" : null,
      rejectedBlocks === undefined ? "rejectedBlockCount" : null,
      claimReports.length !== 1 ? "claimReportRefs" : null,
      blockReports.length !== 1 ? "blockReportRefs" : null,
      verifiedAt === undefined ? "verifiedAt" : null,
    ].filter((item): item is string => Boolean(item));
    if (missing.length) {
      return {
        outcome: "unknown",
        status: null,
        completeness: "incomplete",
        detail: `发布校验记录不完整：${missing.join("、")}。`,
      };
    }
    const hasFindings = (rejectedBlocks ?? 0) > 0 || (vetoedBlocks ?? 0) > 0;
    return lifecycleState(
      "complete",
      hasFindings ? "findings" : "recorded",
      [
        `客户投影回答块 ${publishedBlockCount}`,
        `核验通过回答块 ${acceptedBlocks}`,
        rejectedBlocks === undefined ? null : `核验发现回答块 ${rejectedBlocks}`,
        vetoedBlocks === undefined ? null : `语义风险记录 ${vetoedBlocks}`,
        accepted === undefined ? null : `有证据支持结论 ${accepted}`,
        vetoed === undefined ? null : `结论风险记录 ${vetoed}`,
        `后台报告 ${reportCount}`,
      ].filter(Boolean).join(" · "),
      verifiedAt,
    );
  }
  if (reportCount === 0 && accepted === undefined && vetoed === undefined) return unknownLifecycle();
  const state = lifecycleState(
    reportCount > 0 ? "complete" : "unknown",
    reportCount > 0 ? "recorded" : "unknown",
    [
      accepted === undefined ? null : `已验收结论 ${accepted}`,
      vetoed === undefined ? null : `已拒绝结论 ${vetoed}`,
      reportCount ? `校验报告 ${reportCount}` : null,
    ].filter(Boolean).join(" · "),
    verifiedAt,
  );
  return reportCount > 0 && verifiedAt === undefined
    ? { ...state, completeness: "incomplete" }
    : state;
}

function projectHumanReview(
  value: JsonObject | undefined,
  published: boolean,
): TraceHumanReview {
  if (!published) return { status: "not_available", evaluationCount: 0 };
  if (!value) return { status: "pending", evaluationCount: 0 };
  const status = value.status;
  const evaluationCount = optionalCount(value.evaluationCount);
  if (
    !["pending", "reviewed", "revision_requested"].includes(String(status))
    || evaluationCount === undefined
  ) throw new Error("workbench_human_review_invalid");
  if (status === "pending") {
    if (evaluationCount !== 0 || value.latest !== undefined) {
      throw new Error("workbench_human_review_invalid");
    }
    return { status, evaluationCount } as TraceHumanReview;
  }
  const latest = requiredObject(value.latest, "workbench_human_review_invalid");
  const reviewerRef = requiredString(
    latest.reviewerRef,
    "workbench_human_review_invalid",
  );
  const result = latest.result;
  if (
    result !== "retain_publication"
    && result !== "request_independent_narrative_attempt"
  ) throw new Error("workbench_human_review_invalid");
  const scores = numericRecord(latest.scores, "workbench_human_review_invalid");
  const humanReasons = stringRecord(
    latest.humanReasons,
    "workbench_human_review_invalid",
  );
  const reviewedAt = requiredTimestamp(
    latest.reviewedAt,
    "workbench_human_review_invalid",
  );
  if (
    evaluationCount < 1
    || (status === "revision_requested")
      !== (result === "request_independent_narrative_attempt")
  ) throw new Error("workbench_human_review_invalid");
  return {
    status: status as "reviewed" | "revision_requested",
    evaluationCount,
    latest: { reviewerRef, scores, humanReasons, result, reviewedAt },
  };
}

function numericRecord(value: unknown, error: string): Record<string, number> {
  const record = requiredObject(value, error);
  if (Object.values(record).some((item) => !Number.isInteger(item))) {
    throw new Error(error);
  }
  return Object.fromEntries(
    Object.entries(record).map(([key, item]) => [key, Number(item)]),
  );
}

function stringRecord(value: unknown, error: string): Record<string, string> {
  const record = requiredObject(value, error);
  if (Object.values(record).some((item) => typeof item !== "string")) {
    throw new Error(error);
  }
  return Object.fromEntries(
    Object.entries(record).map(([key, item]) => [key, String(item)]),
  );
}

function settleRunOutcome(
  lifecycle: TraceRun["lifecycle"],
  rawRunStatus: string,
): TraceRunOutcome {
  if (rawRunStatus === "failed") return "failed";
  if ([lifecycle.execution, lifecycle.publication]
    .some((state) => state.outcome === "failed")) return "failed";
  if (
    lifecycle.delivery.outcome === "failed"
    && lifecycle.publication.outcome === "complete"
  ) return "delivery_failed";
  if (lifecycle.delivery.status === "published") return "published";
  if (
    lifecycle.publication.status === "published"
    && lifecycle.delivery.outcome === "pending"
  ) return "delivery_pending";
  if (lifecycle.publication.outcome === "blocked") return "withheld";
  if (lifecycle.execution.outcome === "checkpoint") return "checkpoint";
  if (lifecycle.execution.outcome === "not_applicable"
    && lifecycle.execution.status === "interaction_completed") return "interaction_completed";
  if (lifecycle.execution.outcome === "running") return "running";
  if (lifecycle.execution.outcome === "pending") return "waiting";
  if (lifecycle.execution.outcome === "complete") return "completed";
  return "unknown";
}

function traceCompleteness({
  trace,
  claimsPresent,
  evidenceCompleteness,
}: {
  trace: ReturnType<typeof projectTraceRows>;
  claimsPresent: boolean;
  evidenceCompleteness: TraceCompleteness;
}): TraceRun["traceCompleteness"] {
  return {
    chronology: trace.chronologyCompleteness,
    llmCalls: trace.llmCompleteness,
    acceptedGraph: trace.graphCompleteness,
    claims: claimsPresent ? "known" : "unknown",
    evidence: trace.nodes.some((node) => node.evidenceCompleteness === "incomplete")
      ? "incomplete"
      : evidenceCompleteness,
    timing: trace.timing.completeness,
  };
}

function projectClaims(
  publication: CustomerPublication | undefined,
  links: JsonObject[] | undefined,
): TraceClaim[] {
  if (links === undefined) return [];
  const linkByClaim = new Map(
    links.map((link) => [requiredClaimRef(link), link] as const),
  );
  const textByClaim = new Map<string, string>();
  for (const block of publication?.blocks ?? []) {
    for (const claimRef of block.claim_refs) textByClaim.set(claimRef, block.text);
  }
  const visibleLinks = publication
    ? publication.claim_refs.map((claimRef) => {
        const link = linkByClaim.get(claimRef);
        if (!link) throw new Error("workbench_published_claim_link_missing");
        return link;
      })
    : links;
  return visibleLinks.map((link) => {
    const claimRef = requiredClaimRef(link);
    const text = textByClaim.get(claimRef);
    if (publication && !text) {
      throw new Error("workbench_published_claim_block_missing");
    }
    return {
      claimRef,
      claimClass: optionalString(link.claim_class),
      status: optionalString(link.claim_status),
      text: text ?? claimRef,
      evidenceRefs: requiredStringArray(
        link.evidence_refs,
        "workbench_claim_evidence_link_invalid",
      ),
    };
  });
}

function projectEvidence(rows: JsonObject[] | undefined): TraceEvidence[] {
  if (rows === undefined) return [];
  return rows.map((row) => {
    const evidenceRef = requiredEvidenceRef(row);
    const capability = requiredString(
      row.capability_id,
      "workbench_evidence_projection_invalid",
    );
    const evidenceKind = requiredString(
      row.evidence_kind,
      "workbench_evidence_projection_invalid",
    );
    const strength = requiredString(
      row.maximum_claim_strength ?? row.evidence_strength,
      "workbench_evidence_projection_invalid",
    );
    const dataContractState = requiredString(
      row.data_contract_state,
      "workbench_evidence_projection_invalid",
    );
    const executionState = requiredEvidenceExecutionState(row.execution_state);
    const planRevisionId = requiredString(
      row.plan_revision_id,
      "workbench_evidence_projection_invalid",
    );
    const planState = requiredEvidencePlanState(row.plan_state);
    const taskId = requiredString(
      row.task_id,
      "workbench_evidence_projection_invalid",
    );
    const bindingState = requiredEvidenceBindingState(row.binding_state);
    const executionTransitionAttemptId = optionalString(
      row.execution_transition_attempt_id,
    );
    if (
      (bindingState === "bound" && !executionTransitionAttemptId)
      || (bindingState === "unsettled" && executionTransitionAttemptId)
    ) throw new Error("workbench_evidence_projection_invalid");
    const limitations = requiredStringArray(
      row.limitation_refs,
      "workbench_evidence_projection_invalid",
    );
    return {
      capability,
      label: evidenceKind,
      detail: `数据合同 ${dataContractState} · 可支持强度 ${strength}`,
      strength,
      executionState,
      planRevisionId,
      planState,
      taskId,
      bindingState,
      ...(executionTransitionAttemptId ? { executionTransitionAttemptId } : {}),
      limitations,
      limitationsCompleteness: "known",
      evidenceRef,
    };
  });
}

function runtimeMessages(input: RuntimeTraceProjectionInput, nodes: TraceNode[]) {
  const clarification = isJsonObject(input.request?.clarification)
    ? input.request?.clarification
    : undefined;
  const question = clarificationQuestion(clarification);
  const interaction = isJsonObject(input.request?.interaction_result)
    ? input.request?.interaction_result
    : undefined;
  return [
    { id: "user-question", role: "user" as const, text: input.question, title: "用户" },
    ...(question
      ? [{
          id: "clarification",
          role: "assistant" as const,
          nodeId: findNodeId(nodes, "persist_waiting_for_decision"),
          title: "需要确认",
          text: question,
        }]
      : []),
    ...(typeof interaction?.response_text === "string"
      ? [{
          id: "interaction-result",
          role: "assistant" as const,
          title: "交互响应",
          text: interaction.response_text,
        }]
      : []),
  ];
}

function runtimeBusinessThreads(request: JsonObject | undefined) {
  const plan = isJsonObject(request?.plan_result_refs) ? request?.plan_result_refs : undefined;
  const coverage = isJsonObject(request?.claim_coverage_refs)
    ? request?.claim_coverage_refs
    : undefined;
  return [
    ...(plan && typeof plan.plan_revision_id === "string"
      ? [{
          label: "分析计划",
          value: plan.plan_revision_id,
          detail: typeof plan.authority_context_ref === "string"
            ? plan.authority_context_ref
            : undefined,
        }]
      : []),
    ...(coverage && typeof coverage.decision === "string"
      ? [{
          label: "覆盖检查",
          value: coverage.decision,
          detail: typeof coverage.claim_coverage_checkpoint_ref === "string"
            ? coverage.claim_coverage_checkpoint_ref
            : undefined,
        }]
      : []),
  ];
}

function lifecycleCards(lifecycle: TraceRun["lifecycle"]) {
  return [
    { label: "执行", value: lifecycleValue(lifecycle.execution) },
    { label: "校验", value: lifecycleValue(lifecycle.verifier) },
    { label: "发布", value: lifecycleValue(lifecycle.publication) },
    { label: "交付", value: lifecycleValue(lifecycle.delivery) },
  ];
}

function lifecycleValue(state: TraceLifecycleState) {
  return state.completeness === "unknown" ? "未记录" : state.status ?? "未记录";
}

function lifecycleState(
  outcome: TraceLifecycleOutcome,
  status: string,
  detail?: string,
  at?: string,
): TraceLifecycleState {
  return {
    outcome,
    status,
    completeness: "known",
    ...(detail ? { detail } : {}),
    ...(optionalTimestamp(at) ? { at: optionalTimestamp(at) } : {}),
  };
}

function unknownLifecycle(): TraceLifecycleState {
  return { outcome: "unknown", status: null, completeness: "unknown" };
}

function lifecycleOutcome(status: string): TraceLifecycleOutcome {
  if (["complete", "completed", "passed", "published", "delivered"].includes(status)) return "complete";
  if (["pending", "queued", "running", "retrying"].includes(status)) return "pending";
  if (["withheld", "blocked", "boundary_only"].includes(status)) return "blocked";
  if (["failed", "retryable_failed", "permanently_failed"].includes(status)) return "failed";
  return "unknown";
}

function nodeOutcome(status: string): TraceNodeOutcome {
  if (["completed", "complete", "succeeded", "accepted", "published"].includes(status)) return "completed";
  if (["failed", "rejected", "retryable_failed", "permanently_failed"].includes(status)) return "failed";
  if (["waiting", "waiting_for_user", "waiting_for_clarification", "pending", "queued", "running"].includes(status)) return "waiting";
  if (["skipped", "not_applicable"].includes(status)) return "skipped";
  return "unknown";
}

function transitionOwner(
  nodeName: string,
  providerRef: string | undefined,
  stageTiming: JsonObject | undefined,
): TraceOwner {
  if (providerRef === "user_protocol") return "用户";
  if (stageTiming) {
    if (nodeName === "evaluate_claim_coverage" && providerRef === "local_deterministic") {
      throw new Error("workbench_stage_call_contract_invalid");
    }
    const llmCallCount = requiredNonNegativeInteger(
      stageTiming.llm_call_count,
      "workbench_stage_timing_invalid",
    );
    const controlCallCount = requiredNonNegativeInteger(
      stageTiming.control_call_count,
      "workbench_stage_timing_invalid",
    );
    const queryCallCount = requiredNonNegativeInteger(
      stageTiming.query_call_count,
      "workbench_stage_timing_invalid",
    );
    const capabilityCallCount = requiredNonNegativeInteger(
      stageTiming.capability_call_count,
      "workbench_stage_timing_invalid",
    );
    if (MIXED_TRANSITION_NODES.has(nodeName)) {
      if (
        llmCallCount === 0
        || controlCallCount > 0
        || queryCallCount > 0
        || capabilityCallCount > 0
      ) throw new Error("workbench_stage_call_contract_invalid");
      return "混合";
    }
    if (nodeName === "execute_capability_dag") {
      if (llmCallCount > 0 || controlCallCount > 0) {
        throw new Error("workbench_stage_call_contract_invalid");
      }
      return "本地系统";
    }
    if (nodeName === "evaluate_claim_coverage") {
      if (
        llmCallCount === 0
        || controlCallCount > 0
        || queryCallCount > 0
        || capabilityCallCount > 0
      ) throw new Error("workbench_stage_call_contract_invalid");
      return "混合";
    }
    if (LLM_TRANSITION_NODES.has(nodeName)) {
      if (queryCallCount > 0 || capabilityCallCount > 0) {
        throw new Error("workbench_stage_call_contract_invalid");
      }
      if (nodeName === "conversation_entry" && llmCallCount === 0 && controlCallCount > 0) {
        return "本地系统";
      }
      if (llmCallCount > 0 && controlCallCount === 0) return "LLM";
      throw new Error("workbench_stage_call_contract_invalid");
    }
    throw new Error("workbench_stage_call_contract_invalid");
  }
  if (DURABLE_TIMING_REQUIRED_NODES.has(nodeName)) return "未知";
  if (nodeName === "evaluate_claim_coverage") {
    return providerRef === "local_deterministic" ? "本地系统" : "未知";
  }
  if (LOCAL_SYSTEM_TRANSITION_NODES.has(nodeName)) return "本地系统";
  if (providerRef === undefined) return "未知";
  return "本地系统";
}

function isAcceptedTransition(row: JsonObject) {
  return row.acceptance_state === "accepted";
}

function transitionRequiresDurableTiming(transition: JsonObject) {
  const nodeName = stringValue(transition.node_name);
  return DURABLE_TIMING_REQUIRED_NODES.has(nodeName)
    || (
      nodeName === "evaluate_claim_coverage"
      && optionalString(transition.provider_ref) !== "local_deterministic"
    );
}


function hasCompleteNodeChronology(nodes: TraceNode[]) {
  let previousFinishedAt = Number.NEGATIVE_INFINITY;
  for (const node of nodes) {
    const startedAt = timestampMs(node.startedAt);
    const finishedAt = timestampMs(node.finishedAt);
    if (
      startedAt === undefined
      || finishedAt === undefined
      || finishedAt < startedAt
      || startedAt < previousFinishedAt
    ) return false;
    previousFinishedAt = finishedAt;
  }
  return true;
}

function chronologyDurationMs(nodes: TraceNode[]) {
  const starts = nodes.flatMap((node) => {
    const value = timestampMs(node.startedAt);
    return value === undefined ? [] : [value];
  });
  const finishes = nodes.flatMap((node) => {
    const value = timestampMs(node.finishedAt);
    return value === undefined ? [] : [value];
  });
  if (!starts.length || !finishes.length) return undefined;
  const duration = Math.max(...finishes) - Math.min(...starts);
  return duration > 0 ? duration : undefined;
}

function runtimeNodeProjection(nodeName: string) {
  const projections: Record<string, { label: string; summary: string }> = {
    conversation_entry: { label: "接收业务问题", summary: "已建立本轮对话入口与运行身份。" },
    bind_intent: { label: "绑定业务意图", summary: "已将业务表达绑定到结构化意图、指标和时间语义。" },
    generate_clarification: { label: "生成澄清选项", summary: "已为会改变结论的歧义生成业务选项。" },
    persist_waiting_for_decision: { label: "等待业务确认", summary: "澄清问题已持久化，运行等待用户选择。" },
    accept_material_decision: { label: "接受业务决定", summary: "用户选择已进入 Decision Ledger。" },
    compile_authoritative_plan: { label: "编译权威分析计划", summary: "已绑定分析任务、证据义务和能力执行图。" },
    execute_capability_dag: { label: "执行证据能力", summary: "已按权威计划执行能力任务并持久化证据。" },
    evaluate_claim_coverage: { label: "检查结论证据覆盖", summary: "已检查未解决结论是否仍有可执行、可验收的证据路径。" },
    compile_plan_patch: { label: "扩展分析计划", summary: "已把增量证据路径编译为新的权威计划版本。" },
    settle_claim_authority: { label: "结算结论权威", summary: "已完成结论证据绑定、语义校验和发布强度结算。" },
    compose_claim_aware_narrative: { label: "生成权威回答", summary: "已在结论权威和可见性边界内生成并校验回答。" },
    deliver_publication: { label: "交付客户发布", summary: "已交付唯一客户安全投影并记录交付结果。" },
  };
  return projections[nodeName] ?? {
    label: nodeName,
    summary: "该权威工作流节点已完成持久化。",
  };
}

function validateCustomerPublication(value: unknown): CustomerPublication {
  return parseCustomerPublication(value);
}

function validatePublicationRefs(value: SafePublicationRefs) {
  const digests = [
    value.authority_bundle_digest,
    value.publication_digest,
    value.projection_digest,
  ];
  const authoritySealedAt = optionalTimestamp(value.authority_sealed_at);
  const publishedAt = optionalTimestamp(value.published_at);
  const deliveryAttemptedAt = optionalTimestamp(value.delivery_attempted_at);
  if (
    !value.authority_bundle_ref
    || !value.publication_ref
    || !value.projection_id
    || !value.outbox_ref
    || authoritySealedAt === undefined
    || publishedAt === undefined
    || !digests.every((digest) => /^[0-9a-f]{64}$/.test(digest))
    || !["pending", "published", "retryable_failed", "permanently_failed"].includes(
      value.delivery_status,
    )
    || (value.delivery_status === "pending" && deliveryAttemptedAt !== undefined)
    || (value.delivery_status !== "pending" && deliveryAttemptedAt === undefined)
  ) throw new Error("publication_authority_invalid");
}

function canonicalScore(run: TraceRun) {
  const outcomeScore: Record<TraceRunOutcome, number> = {
    published: 50,
    completed: 40,
    checkpoint: 35,
    interaction_completed: 34,
    delivery_pending: 45,
    delivery_failed: 25,
    withheld: 30,
    failed: 20,
    running: 15,
    waiting: 10,
    unknown: 0,
  };
  return outcomeScore[run.runOutcome]
    + Object.values(run.traceCompleteness).filter((value) => value === "known").length
    + (run.answer ? 1 : 0);
}

function runOutcomeLabel(outcome: TraceRunOutcome) {
  const labels: Record<TraceRunOutcome, string> = {
    published: "已发布",
    completed: "执行完成",
    checkpoint: "阶段检查点",
    interaction_completed: "交互已完成",
    delivery_pending: "等待交付",
    delivery_failed: "交付失败",
    withheld: "发布受限",
    failed: "运行失败",
    running: "执行中",
    waiting: "等待继续",
    unknown: "状态未记录",
  };
  return labels[outcome];
}

function requestStatus(request: JsonObject | undefined, key: string) {
  return optionalString(request?.[key]);
}

function clarificationQuestion(value: JsonObject | undefined) {
  const questions = value?.questions;
  if (!Array.isArray(questions) || !isJsonObject(questions[0])) return undefined;
  const question = optionalString(questions[0].question);
  if (!question) return undefined;
  const options = Array.isArray(questions[0].options)
    ? questions[0].options.flatMap((option) => {
        if (!isJsonObject(option)) return [];
        const label = optionalString(option.label);
        const description = optionalString(option.description);
        return label && description ? [`${label}：${description}`] : [];
      })
    : [];
  return [question, ...options].join("\n");
}

function findNodeId(nodes: TraceNode[], nodeName: string) {
  return nodes.find((node) => node.node === nodeName)?.id;
}

function requiredClaimRef(value: JsonObject) {
  return requiredString(value.claim_ref, "workbench_claim_evidence_link_invalid");
}

function requiredEvidenceRef(value: JsonObject) {
  return requiredString(value.evidence_ref, "workbench_evidence_projection_invalid");
}

function requiredEvidenceExecutionState(value: unknown): TraceEvidenceExecutionState {
  const state = requiredString(value, "workbench_evidence_projection_invalid");
  if (!EVIDENCE_EXECUTION_STATES.has(state as TraceEvidenceExecutionState)) {
    throw new Error("workbench_evidence_projection_invalid");
  }
  return state as TraceEvidenceExecutionState;
}

function requiredEvidencePlanState(value: unknown): TraceEvidencePlanState {
  const state = requiredString(value, "workbench_evidence_projection_invalid");
  if (!EVIDENCE_PLAN_STATES.has(state as TraceEvidencePlanState)) {
    throw new Error("workbench_evidence_projection_invalid");
  }
  return state as TraceEvidencePlanState;
}

function requiredEvidenceBindingState(value: unknown): TraceEvidenceBindingState {
  const state = requiredString(value, "workbench_evidence_projection_invalid");
  if (!EVIDENCE_BINDING_STATES.has(state as TraceEvidenceBindingState)) {
    throw new Error("workbench_evidence_projection_invalid");
  }
  return state as TraceEvidenceBindingState;
}

function evidenceTraceCompleteness(rows: JsonObject[] | undefined): TraceCompleteness {
  if (rows === undefined) return "unknown";
  return rows.some((row) => requiredEvidenceBindingState(row.binding_state) === "unsettled")
    ? "incomplete"
    : "known";
}

function sameStringSet(left: string[], right: string[]) {
  return left.length === right.length
    && new Set(left).size === left.length
    && new Set(right).size === right.length
    && left.every((value) => right.includes(value));
}

function requiredString(value: unknown, error: string) {
  if (typeof value !== "string" || !value.trim()) throw new Error(error);
  return value;
}

function requiredObject(value: unknown, error: string): JsonObject {
  if (!isJsonObject(value)) throw new Error(error);
  return value;
}

function optionalString(value: unknown) {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function stringValue(value: unknown) {
  return typeof value === "string" ? value.trim() : "";
}

function stringArray(value: unknown) {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && Boolean(item.trim()))
    : [];
}

function requiredStringArray(value: unknown, error: string) {
  if (!Array.isArray(value)) throw new Error(error);
  return value.map((item) => requiredString(item, error));
}

function requiredUniqueStringArray(value: unknown, error: string) {
  const items = requiredStringArray(value, error);
  if (new Set(items).size !== items.length) throw new Error(error);
  return items;
}

function optionalCount(value: unknown) {
  const parsed = typeof value === "string" && /^\d+$/.test(value) ? Number(value) : value;
  return Number.isSafeInteger(parsed) && Number(parsed) >= 0 ? Number(parsed) : undefined;
}

function requiredPositiveInteger(value: unknown, error: string) {
  if (!Number.isSafeInteger(value) || Number(value) <= 0) throw new Error(error);
  return Number(value);
}

function requiredNonNegativeInteger(value: unknown, error: string) {
  if (!Number.isSafeInteger(value) || Number(value) < 0) throw new Error(error);
  return Number(value);
}

function optionalTimestamp(value: unknown) {
  if (value instanceof Date && !Number.isNaN(value.getTime())) return value.toISOString();
  if (typeof value !== "string" || timestampMs(value) === undefined) return undefined;
  return value;
}

function requiredTimestamp(value: unknown, error: string) {
  const timestamp = optionalTimestamp(value);
  if (!timestamp) throw new Error(error);
  return timestamp;
}

function timestampMs(value: unknown) {
  if (value instanceof Date) {
    const result = value.getTime();
    return Number.isFinite(result) ? result : undefined;
  }
  if (typeof value !== "string" || !value.trim()) return undefined;
  const result = Date.parse(value);
  return Number.isFinite(result) ? result : undefined;
}

function elapsedMs(start: unknown, finish: unknown) {
  const startMs = timestampMs(start);
  const finishMs = timestampMs(finish);
  if (startMs === undefined || finishMs === undefined || finishMs <= startMs) return undefined;
  return finishMs - startMs;
}

function isJsonObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
