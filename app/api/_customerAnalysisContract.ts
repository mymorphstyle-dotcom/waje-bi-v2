import type { CustomerPublication } from "./_customerPublicationContract";

export const CUSTOMER_ANALYSIS_SCHEMA_VERSION = "customer-conversation.v5" as const;

export const CUSTOMER_PHASES = [
  { id: "understanding", label: "理解业务问题" },
  { id: "planning", label: "整理分析路径" },
  { id: "querying", label: "查询并分析数据" },
  { id: "synthesizing", label: "汇总结论与边界" },
  { id: "delivering", label: "生成业务参考" },
] as const;

export type CustomerPhase = (typeof CUSTOMER_PHASES)[number]["id"];
export type CustomerMainStatus =
  | "idle"
  | "working"
  | "needs_input"
  | "checkpoint"
  | "completed"
  | "completed_with_limits"
  | "failed";

export type CustomerProgressUpdate = {
  key: CustomerPhase;
  text: string;
  status: "completed" | "active" | "failed";
  confirmedAt: string;
};

export type CustomerMessage = {
  key: string;
  role: "user" | "assistant";
  text: string;
  createdAt: string;
};

type CustomerSnapshotMessage = CustomerMessage & {
  itemType?: string;
  operationKey?: string | null;
};

type AgentCompletionKind =
  | "direct_response"
  | "context_response"
  | "tool_response"
  | "analysis_publication"
  | "failed_turn";

export type CustomerInputOption = {
  optionKey: string;
  label: string;
  description: string;
  recommended: boolean;
};

export type CustomerInputQuestion = {
  questionKey: string;
  question: string;
  explanation: string;
  options: CustomerInputOption[];
};

export type CustomerInputRequest = {
  kind: "clarification" | "topic_choice";
  title: string;
  question: string;
  explanation: string;
  options: CustomerInputOption[];
  questions: CustomerInputQuestion[];
  allowFreeform: boolean;
};

export type CustomerAnswerBlock = {
  key: string;
  kind: "summary" | "finding" | "context" | "limitation" | "recommendation";
  heading: string | null;
  text: string;
};

export type CustomerAnswer = {
  blocks: CustomerAnswerBlock[];
  warnings: string[];
  evidenceCount: number;
  limitationCount: number;
};

type CustomerStateBase = {
  status: CustomerMainStatus;
  title: string;
  description: string;
  updates: CustomerProgressUpdate[];
};

export type CustomerAnalysisState =
  | (CustomerStateBase & { status: "idle" })
  | (CustomerStateBase & {
      status: "working";
      phase: CustomerPhase;
      safeToClose: true;
    })
  | (CustomerStateBase & {
      status: "needs_input";
      phase: CustomerPhase;
      input: CustomerInputRequest;
    })
  | (CustomerStateBase & {
      status: "checkpoint";
      phase: CustomerPhase;
      safeToClose: true;
    })
  | (CustomerStateBase & {
      status: "completed" | "completed_with_limits";
      phase: "delivering";
      answer: CustomerAnswer;
    })
  | (CustomerStateBase & {
      status: "failed";
      phase: CustomerPhase;
      recovery: "retry" | "new_analysis" | "contact_support";
    });

export type CustomerAnalysisTransport = {
  threadHandle: string;
  runHandle: string | null;
  actionHandle: string | null;
  actionKind: "agent_pending_action" | "bi_clarification" | "topic_choice" | null;
  eventsUrl: string | null;
  eventCursor: string;
  latestItemSequence: number;
  messageHistory: {
    hasMore: boolean;
    beforeCursor: string | null;
  };
  acceptedOperationIds: string[];
  technicalDetailRef: string | null;
};

export type PlannerIssueState = {
  question: string;
  status: "pending" | "querying" | "evidenced" | "limited";
  statusLabel: string;
};

export type CustomerAnalysisSnapshot = {
  schemaVersion: typeof CUSTOMER_ANALYSIS_SCHEMA_VERSION;
  stateVersion: string;
  confirmedAt: string;
  thread: {
    title: string;
    createdAt: string;
  };
  messages: CustomerMessage[];
  businessUnderstanding: string | null;
  plannerIssues: string[];
  plannerIssueStates: PlannerIssueState[];
  state: CustomerAnalysisState;
  transport: CustomerAnalysisTransport;
};

export type CustomerThreadSummary = {
  title: string;
  status: CustomerMainStatus;
  updatedAt: string;
  transport: { threadHandle: string };
};

export type CustomerApiError = {
  error: {
    code:
      | "sign_in_required"
      | "analysis_not_found"
      | "request_invalid"
      | "request_too_large"
      | "service_busy"
      | "action_in_progress"
      | "action_no_longer_available"
      | "analysis_unavailable";
    title: string;
    message: string;
    recovery: "refresh" | "retry" | "new_analysis" | "contact_support" | "sign_in";
  };
  transport: { technicalDetailRef: string };
};

export type CustomerSnapshotSource = {
  thread: {
    id: string;
    createdAt: string;
  };
  messages: CustomerSnapshotMessage[];
  run: null | {
    id: string;
    status: string;
    request: Record<string, unknown>;
    createdAt: string;
    updatedAt: string;
  };
  runNodes: Array<{
    nodeName: string;
    status: string;
    confirmedAt: string | null;
  }>;
  currentClarification: unknown;
  interactionResult: unknown;
  customerPublication: CustomerPublication | null;
  customerPublicationTaskRef?: string | null;
  acceptedOperationIds: string[];
  progressPhase?: CustomerPhase | null;
  agentHead?: {
    status: CustomerMainStatus;
    activeTaskRef: string | null;
    pendingActionRef: string | null;
  };
  agentTerminal?: {
    status: "completed" | "completed_with_limits" | "failed";
    finalOutput: Record<string, unknown> | null;
    errorCode: string | null;
    completionKind: AgentCompletionKind;
    durableTaskRef: string | null;
    operationId: string;
  } | null;
  pendingAction?: Record<string, unknown> | null;
  confirmedAt: string;
  stateVersion: string;
  eventCursor?: string;
  latestItemSequence?: number;
  messageHistory?: {
    hasMore: boolean;
    beforeCursor: string | null;
  };
};

const PHASE_INDEX = new Map(
  CUSTOMER_PHASES.map((phase, index) => [phase.id, index]),
);

const PHASE_COPY: Record<CustomerPhase, { title: string; description: string }> = {
  understanding: {
    title: "正在理解业务问题",
    description: "正在确认业务目标、分析范围和会影响结论的关键口径。",
  },
  planning: {
    title: "正在整理分析路径",
    description: "正在确定需要查询的数据、比较口径和分析顺序。",
  },
  querying: {
    title: "正在查询和分析数据",
    description: "正在查询数据并寻找能够解释业务变化的模式。",
  },
  synthesizing: {
    title: "正在汇总结论与边界",
    description: "正在组织主要发现、适用范围和建议的下一步行动。",
  },
  delivering: {
    title: "正在生成业务参考",
    description: "分析已经收尾，业务参考即将出现在当前对话中。",
  },
};

const STOPPED_CHECKPOINTS = {
  phase02: {
    runStatus: "planned",
    phase: "planning",
    title: "分析计划已确认",
    description: "待解决问题、分析路径和执行顺序已经保存。",
  },
  phase03: {
    runStatus: "evidence_ready",
    phase: "querying",
    title: "数据分析阶段已完成",
    description: "查询结果和分析证据已经保存。",
  },
  phase04: {
    runStatus: "authority_sealed",
    phase: "synthesizing",
    title: "结论校验阶段已完成",
    description: "结论依据和证据边界已经保存。",
  },
  phase05: {
    runStatus: "narrative_ready",
    phase: "synthesizing",
    title: "业务表达阶段已完成",
    description: "业务结论和适用边界已经生成并保存。",
  },
} as const;

function stoppedCheckpoint(
  runStatus: string,
  request: Record<string, unknown>,
) {
  const stopAfterPhase = stringValue(request.stop_after_phase);
  const checkpoint = STOPPED_CHECKPOINTS[
    stopAfterPhase as keyof typeof STOPPED_CHECKPOINTS
  ];
  return checkpoint?.runStatus === runStatus ? checkpoint : null;
}

export function isTerminalAnalysisCheckpoint(
  runStatus: unknown,
  request: Record<string, unknown>,
) {
  return typeof runStatus === "string"
    && stoppedCheckpoint(runStatus, request) !== null;
}

const NODE_PHASES: Record<string, CustomerPhase> = {
  conversation_entry: "understanding",
  bind_intent: "understanding",
  understand_business_intent: "understanding",
  decide_question_boundary: "understanding",
  clarification_policy_gate: "understanding",
  generate_clarification: "understanding",
  persist_clarification: "understanding",
  persist_waiting_for_decision: "understanding",
  accept_material_decision: "understanding",
  compile_authoritative_plan: "planning",
  compile_plan_patch: "planning",
  execute_capability_dag: "querying",
  evaluate_claim_coverage: "synthesizing",
  settle_claim_authority: "synthesizing",
  compose_claim_aware_narrative: "synthesizing",
  verifier_result: "synthesizing",
  deliver_publication: "delivering",
};

const LIMITATION_BLOCK_ROLES = new Set([
  "boundary",
  "limitation",
  "limitations",
]);
const RECOMMENDATION_BLOCK_ROLES = new Set(["next_action", "recommendation"]);
const CONTEXT_BLOCK_ROLES = new Set(["context", "contextual_pattern"]);
const SUMMARY_BLOCK_ROLES = new Set(["executive_answer", "executive_summary"]);

export function projectCustomerAnalysisSnapshot(
  source: CustomerSnapshotSource,
): CustomerAnalysisSnapshot {
  const agentState = stateFromAgentHead(source);
  const messages = customerConversationMessages(
    source.messages,
    agentState?.ownedMessageKey ?? null,
  );
  const title = conversationTitle(messages);
  const plannerIssues = plannerIssuesFrom(source.run?.request);
  const base = {
    schemaVersion: CUSTOMER_ANALYSIS_SCHEMA_VERSION,
    stateVersion: source.stateVersion,
    confirmedAt: source.confirmedAt,
    thread: { title, createdAt: source.thread.createdAt },
    messages,
    businessUnderstanding: businessUnderstandingFrom(source.run?.request),
    plannerIssues,
    plannerIssueStates: plannerIssueStatesFrom(
      source.run?.request,
      plannerIssues,
    ),
  } as const;

  if (agentState) {
    return {
      ...base,
      state: agentState.state,
      transport: transportFrom(
        source,
        agentState.runHandle,
        agentState.actionHandle,
      ),
    };
  }

  if (!source.run) {
    return {
      ...base,
      state: {
        status: "idle",
        title: "准备开始分析",
        description: "输入业务问题后，分析会在后台持续运行并保存当前状态。",
        updates: [],
      },
      transport: transportFrom(source, null, null),
    };
  }

  const run = source.run;
  const phase = currentPhase(run.status, source.runNodes, source.progressPhase);
  const checkpoint = stoppedCheckpoint(run.status, run.request);
  const interaction = customerInteraction(source.interactionResult);
  const clarification = run.status === "waiting_for_clarification"
    ? customerClarification(source.currentClarification)
    : null;
  let state: CustomerAnalysisState;
  let actionHandle: string | null = null;

  if (run.status === "waiting_for_clarification") {
    if (!clarification) throw new Error("customer_current_clarification_missing");
    actionHandle = run.id;
    state = {
      status: "needs_input",
      phase: "understanding",
      title: "需要你的确认",
      description: "这个选择会显著影响分析结论。提交后，分析会从当前运行继续。",
      updates: progressUpdates(
        "understanding",
        "needs_input",
        source.runNodes,
        run.updatedAt,
      ),
      input: clarification,
    };
  } else if (run.status === "interaction_completed") {
    if (!interaction) throw new Error("customer_interaction_result_missing");
    if (interaction.kind === "input") {
      actionHandle = run.id;
      state = {
        status: "needs_input",
        phase: "understanding",
        title: "请选择要继续的主题",
        description: "当前消息可能对应多个分析方向，需要先确认接下来处理哪一项。",
        updates: progressUpdates(
          "understanding",
          "needs_input",
          source.runNodes,
          run.updatedAt,
        ),
        input: interaction.input,
      };
    } else {
      state = completedInteractionState(interaction.text, run.updatedAt);
    }
  } else if (run.status === "completed") {
    state = completedRunState(
      source.customerPublication,
      run.request,
      source.runNodes,
      run.updatedAt,
    );
  } else if (run.status === "failed") {
    state = failedState(phase, run.request, source.runNodes, run.updatedAt);
  } else if (checkpoint) {
    state = {
      status: "checkpoint",
      phase: checkpoint.phase,
      title: checkpoint.title,
      description: checkpoint.description,
      updates: progressUpdates(
        checkpoint.phase,
        "completed",
        source.runNodes,
        run.updatedAt,
      ),
      safeToClose: true,
    };
  } else if ([
    "queued",
    "running",
    "running_workflow",
    "planned",
    "evidence_ready",
    "authority_sealed",
    "narrative_ready",
  ].includes(run.status)) {
    state = {
      status: "working",
      phase,
      title: PHASE_COPY[phase].title,
      description: PHASE_COPY[phase].description,
      updates: progressUpdates(phase, "working", source.runNodes, run.updatedAt),
      safeToClose: true,
    };
  } else {
    throw new Error(`customer_run_status_unmapped:${run.status}`);
  }

  return {
    ...base,
    state,
    transport: transportFrom(source, run.id, actionHandle),
  };
}

export function parseCustomerAnalysisSnapshot(value: unknown): CustomerAnalysisSnapshot {
  const snapshot = requiredObject(value, "customer_snapshot_invalid");
  assertExactKeys(snapshot, [
    "schemaVersion",
    "stateVersion",
    "confirmedAt",
    "thread",
    "messages",
    "businessUnderstanding",
    "plannerIssues",
    "plannerIssueStates",
    "state",
    "transport",
  ], "customer_snapshot_invalid");
  if (snapshot.schemaVersion !== CUSTOMER_ANALYSIS_SCHEMA_VERSION) {
    throw new Error("customer_snapshot_version_unsupported");
  }
  const parsed = snapshot as unknown as CustomerAnalysisSnapshot;
  requiredString(parsed.stateVersion, "customer_snapshot_invalid");
  requiredTimestamp(parsed.confirmedAt, "customer_snapshot_invalid");
  requiredObject(parsed.thread, "customer_snapshot_invalid");
  assertExactKeys(parsed.thread, ["title", "createdAt"], "customer_snapshot_invalid");
  requiredString(parsed.thread.title, "customer_snapshot_invalid");
  requiredTimestamp(parsed.thread.createdAt, "customer_snapshot_invalid");
  if (!Array.isArray(parsed.messages)) throw new Error("customer_snapshot_invalid");
  parsed.messages.forEach((message) => {
    assertExactKeys(message, ["key", "role", "text", "createdAt"], "customer_snapshot_invalid");
    requiredString(message.key, "customer_snapshot_invalid");
    if (message.role !== "user" && message.role !== "assistant") {
      throw new Error("customer_snapshot_invalid");
    }
    requiredString(message.text, "customer_snapshot_invalid");
    requiredTimestamp(message.createdAt, "customer_snapshot_invalid");
  });
  if (parsed.businessUnderstanding !== null) {
    requiredString(parsed.businessUnderstanding, "customer_snapshot_invalid");
  }
  if (
    !Array.isArray(parsed.plannerIssues)
    || parsed.plannerIssues.some(
      (issue) => typeof issue !== "string" || !issue.trim(),
    )
  ) {
    throw new Error("customer_snapshot_invalid");
  }
  if (!Array.isArray(parsed.plannerIssueStates)) {
    throw new Error("customer_snapshot_invalid");
  }
  parsed.plannerIssueStates.forEach((issue) => {
    assertExactKeys(
      issue,
      ["question", "status", "statusLabel"],
      "customer_snapshot_invalid",
    );
    requiredString(issue.question, "customer_snapshot_invalid");
    requiredString(issue.statusLabel, "customer_snapshot_invalid");
    if (!["pending", "querying", "evidenced", "limited"].includes(issue.status)) {
      throw new Error("customer_snapshot_invalid");
    }
  });
  validateState(parsed.state);
  validateTransport(parsed.transport);
  return parsed;
}

function businessUnderstandingFrom(
  request: Record<string, unknown> | undefined,
) {
  const value = request?.business_understanding;
  const intentRevisionId = request?.business_understanding_intent_revision_id;
  return typeof value === "string"
    && value.trim() === value
    && value
    && typeof intentRevisionId === "string"
    && intentRevisionId.trim() === intentRevisionId
    && intentRevisionId
    ? value
    : null;
}

function plannerIssuesFrom(
  request: Record<string, unknown> | undefined,
): string[] {
  const projection = optionalObject(request?.planner_problem_projection);
  const planRefs = optionalObject(request?.plan_result_refs);
  if (
    projection?.schema_version !== "planner-problem-projection.v1"
    || !planRefs
    || stringValue(projection.plan_revision_id)
      !== stringValue(planRefs.plan_revision_id)
    || stringValue(projection.planner_proposal_id)
      !== stringValue(planRefs.planner_proposal_id)
    || !Array.isArray(projection.issues)
  ) return [];
  const issues = projection.issues.flatMap((value) => {
    const issue = optionalObject(value);
    const question = stringValue(issue?.question).trim();
    const issueId = stringValue(issue?.issue_id);
    if (!issueId || !question) return [];
    return [question];
  });
  return [...new Set(issues)];
}

function plannerIssueStatesFrom(
  request: Record<string, unknown> | undefined,
  plannerIssues: string[],
): PlannerIssueState[] {
  const projection = optionalObject(request?.query_bundle_projection);
  const planRefs = optionalObject(request?.plan_result_refs);
  if (
    projection?.schema_version !== "query-bundle-projection.v1"
    || !planRefs
    || stringValue(projection.plan_revision_id)
      !== stringValue(planRefs.plan_revision_id)
    || stringValue(projection.planner_proposal_id)
      !== stringValue(planRefs.planner_proposal_id)
    || !Array.isArray(projection.issues)
  ) {
    return plannerIssues.map((question) => ({
      question,
      status: "pending",
      statusLabel: "待分析",
    }));
  }
  const byQuestion = new Map<string, PlannerIssueState>();
  projection.issues.forEach((value) => {
    const issue = optionalObject(value);
    const question = stringValue(issue?.question).trim();
    const status = stringValue(issue?.status);
    const statusLabel = stringValue(issue?.status_message).trim();
    if (
      question
      && statusLabel
      && ["pending", "querying", "evidenced", "limited"].includes(status)
    ) {
      byQuestion.set(question, {
        question,
        status: status as PlannerIssueState["status"],
        statusLabel,
      });
    }
  });
  return plannerIssues.map((question) => byQuestion.get(question) ?? {
    question,
    status: "pending",
    statusLabel: "待分析",
  });
}

export function parseCustomerThreadSummaries(value: unknown): CustomerThreadSummary[] {
  const root = requiredObject(value, "customer_thread_list_invalid");
  if (!Array.isArray(root.threads)) throw new Error("customer_thread_list_invalid");
  return root.threads.map((item) => {
    const summary = requiredObject(item, "customer_thread_list_invalid") as CustomerThreadSummary;
    requiredString(summary.title, "customer_thread_list_invalid");
    requiredTimestamp(summary.updatedAt, "customer_thread_list_invalid");
    if (!isMainStatus(summary.status)) throw new Error("customer_thread_list_invalid");
    const transport = requiredObject(summary.transport, "customer_thread_list_invalid");
    requiredString(transport.threadHandle, "customer_thread_list_invalid");
    return summary;
  });
}

export function parseCustomerApiError(value: unknown): CustomerApiError {
  const root = requiredObject(value, "customer_error_invalid") as CustomerApiError;
  const error = requiredObject(root.error, "customer_error_invalid");
  const transport = requiredObject(root.transport, "customer_error_invalid");
  assertExactKeys(root, ["error", "transport"], "customer_error_invalid");
  assertExactKeys(
    error,
    ["code", "title", "message", "recovery"],
    "customer_error_invalid",
  );
  assertExactKeys(
    transport,
    ["technicalDetailRef"],
    "customer_error_invalid",
  );
  if (![
    "sign_in_required",
    "analysis_not_found",
    "request_invalid",
    "request_too_large",
    "service_busy",
    "action_in_progress",
    "action_no_longer_available",
    "analysis_unavailable",
  ].includes(requiredString(error.code, "customer_error_invalid"))) {
    throw new Error("customer_error_invalid");
  }
  requiredString(error.title, "customer_error_invalid");
  requiredString(error.message, "customer_error_invalid");
  if (![
    "refresh",
    "retry",
    "new_analysis",
    "contact_support",
    "sign_in",
  ].includes(requiredString(error.recovery, "customer_error_invalid"))) {
    throw new Error("customer_error_invalid");
  }
  requiredString(transport.technicalDetailRef, "customer_error_invalid");
  return root;
}

function currentPhase(
  status: string,
  nodes: CustomerSnapshotSource["runNodes"],
  persistedPhase?: CustomerPhase | null,
): CustomerPhase {
  if (status === "waiting_for_clarification" || status === "queued") {
    return "understanding";
  }
  let best: CustomerPhase = status === "completed"
    ? "delivering"
    : status === "evidence_ready" || status === "authority_sealed"
        || status === "narrative_ready"
      ? "synthesizing"
      : status === "planned"
        ? "planning"
        : "understanding";
  if (
    persistedPhase
    && (PHASE_INDEX.get(persistedPhase) ?? 0) > (PHASE_INDEX.get(best) ?? 0)
  ) {
    best = persistedPhase;
  }
  for (const node of nodes) {
    const candidate = NODE_PHASES[node.nodeName];
    if (
      candidate
      && (PHASE_INDEX.get(candidate) ?? 0) > (PHASE_INDEX.get(best) ?? 0)
    ) {
      best = candidate;
    }
  }
  return best;
}

function progressUpdates(
  current: CustomerPhase,
  status: "working" | "needs_input" | "completed" | "failed",
  nodes: CustomerSnapshotSource["runNodes"],
  fallbackTime: string,
): CustomerProgressUpdate[] {
  const currentIndex = PHASE_INDEX.get(current) ?? 0;
  const confirmedByPhase = new Map<CustomerPhase, string>();
  for (const node of nodes) {
    const phase = NODE_PHASES[node.nodeName];
    if (!phase || !node.confirmedAt) continue;
    const previous = confirmedByPhase.get(phase);
    if (!previous || Date.parse(node.confirmedAt) > Date.parse(previous)) {
      confirmedByPhase.set(phase, node.confirmedAt);
    }
  }
  return CUSTOMER_PHASES.slice(0, currentIndex + 1).map((phase, index) => ({
    key: phase.id,
    text: phase.label,
    status: status === "completed" || index < currentIndex
      ? "completed"
      : status === "failed"
        ? "failed"
        : "active",
    confirmedAt: confirmedByPhase.get(phase.id) ?? fallbackTime,
  }));
}

function completedRunState(
  publication: CustomerPublication | null,
  request: Record<string, unknown>,
  nodes: CustomerSnapshotSource["runNodes"],
  confirmedAt: string,
): CustomerAnalysisState {
  const postExecutionStatus = stringValue(request.post_execution_status);
  const publicationStatus = stringValue(request.publication_status);
  const deliveryStatus = stringValue(request.delivery_status);
  if (
    !publication
    || postExecutionStatus !== "completed"
    || publicationStatus !== "published"
    || deliveryStatus !== "published"
  ) {
    return failedState("delivering", request, nodes, confirmedAt);
  }
  const answer = answerFromPublication(publication);
  const hasLimits = answer.limitationCount > 0
    || answer.warnings.length > 0
    || stringValue(request.analysis_status) === "boundary_only";
  return {
    status: hasLimits ? "completed_with_limits" : "completed",
    phase: "delivering",
    title: hasLimits ? "业务参考已生成，结论有适用边界" : "业务参考已生成",
    description: hasLimits
      ? "请结合证据边界和限制进行业务判断，重要决策建议由人复核。"
      : "以下内容用于业务判断参考，重要决策建议由人复核。",
    updates: progressUpdates("delivering", "completed", nodes, confirmedAt),
    answer,
  };
}

function completedInteractionState(text: string, confirmedAt: string): CustomerAnalysisState {
  return {
    status: "completed",
    phase: "delivering",
    title: "已完成",
    description: "已生成当前问题的回复。",
    updates: [{
      key: "delivering",
      text: "已生成回复",
      status: "completed",
      confirmedAt,
    }],
    answer: {
      blocks: [{ key: "interaction-response", kind: "summary", heading: null, text }],
      warnings: [],
      evidenceCount: 0,
      limitationCount: 0,
    },
  };
}

function failedState(
  phase: CustomerPhase,
  request: Record<string, unknown>,
  nodes: CustomerSnapshotSource["runNodes"],
  confirmedAt: string,
): CustomerAnalysisState {
  const delivery = stringValue(request.delivery_status);
  if (delivery === "retryable_failed") {
    return {
      status: "failed",
      phase,
      title: "结果交付暂时失败",
      description: "业务参考已经生成，但本次交付没有完成。请重新获取最新状态；若仍未恢复，请开始新分析或联系支持。",
      updates: progressUpdates(phase, "failed", nodes, confirmedAt),
      recovery: "retry",
    };
  }
  return {
    status: "failed",
    phase,
    title: "本次分析未完成",
    description: "本次运行遇到故障，暂时无法生成业务参考。故障已完整记录，可重新发起分析或联系支持。",
    updates: progressUpdates(phase, "failed", nodes, confirmedAt),
    recovery: delivery === "permanently_failed" ? "contact_support" : "new_analysis",
  };
}

function answerFromPublication(publication: CustomerPublication): CustomerAnswer {
  const blocks = publication.blocks.map((block, index) => {
    const kind = answerBlockKind(block.role, block.statement_role, index);
    return {
      key: `answer-${index}`,
      kind,
      heading: answerBlockHeading(block.role, block.statement_role, kind, index),
      text: block.text,
    };
  });
  return {
    blocks,
    warnings: [...new Set(publication.warnings)],
    evidenceCount: publication.claim_refs.length,
    limitationCount: publication.limitation_refs.length,
  };
}

const CUSTOMER_BLOCK_HEADINGS: Readonly<Record<string, string>> = {
  executive_answer: "核心结论",
  executive_summary: "核心结论",
  direction: "变化方向",
  accounting_drivers: "驱动机制",
  dimension_localization: "重点定位",
  contextual_pattern: "补充观察",
  context: "补充观察",
  boundary: "证据边界",
  limitation: "证据边界",
  limitations: "证据边界",
  next_action: "运营建议",
  recommendation: "运营建议",
};

function answerBlockHeading(
  role: string,
  statementRole: string,
  kind: CustomerAnswerBlock["kind"],
  index: number,
): string {
  const declaredHeading = CUSTOMER_BLOCK_HEADINGS[role]
    ?? CUSTOMER_BLOCK_HEADINGS[statementRole];
  if (declaredHeading) return declaredHeading;
  if (kind === "summary" || index === 0) return "核心结论";
  if (kind === "context") return "补充观察";
  if (kind === "limitation") return "证据边界";
  if (kind === "recommendation") return "运营建议";
  return "深入分析";
}

function answerBlockKind(
  role: string,
  statementRole: string,
  index: number,
): CustomerAnswerBlock["kind"] {
  if (LIMITATION_BLOCK_ROLES.has(role)
    || LIMITATION_BLOCK_ROLES.has(statementRole)) {
    return "limitation";
  }
  if (RECOMMENDATION_BLOCK_ROLES.has(role)
    || RECOMMENDATION_BLOCK_ROLES.has(statementRole)) {
    return "recommendation";
  }
  if (CONTEXT_BLOCK_ROLES.has(role)
    || CONTEXT_BLOCK_ROLES.has(statementRole)) {
    return "context";
  }
  if (index === 0 || SUMMARY_BLOCK_ROLES.has(role) || SUMMARY_BLOCK_ROLES.has(statementRole)) {
    return "summary";
  }
  return "finding";
}

function customerClarification(value: unknown): CustomerInputRequest | null {
  const clarification = optionalObject(value);
  if (!clarification || clarification.status !== "waiting") return null;
  if (!Array.isArray(clarification.questions)) return null;
  const questions = clarification.questions.flatMap((rawQuestion) => {
    const value = optionalObject(rawQuestion);
    const questionKey = stringValue(value?.slot_id);
    const question = stringValue(value?.question);
    if (!questionKey || !question || !Array.isArray(value?.options)) return [];
    const options = value.options.flatMap((raw) => {
      const option = optionalObject(raw);
      const optionKey = stringValue(option?.option_id);
      const label = stringValue(option?.label);
      const description = stringValue(option?.description);
      if (!option || !optionKey || !label || !description) return [];
      if (optionKey === "tell_agent_differently") return [];
      return [{
        optionKey,
        label,
        description,
        recommended: option.recommended === true,
      }];
    });
    if (options.length < 2 || options.length > 3) return [];
    return [{
      questionKey,
      question,
      explanation: stringValue(value.recommendation_reason),
      options,
    }];
  });
  if (!questions.length || questions.length !== clarification.questions.length) {
    return null;
  }
  const first = questions[0];
  return {
    kind: "clarification",
    title: "需要确认后继续",
    question: first.question,
    explanation: first.explanation,
    options: first.options,
    questions,
    allowFreeform: clarification.questions.some((rawQuestion) =>
      Array.isArray(optionalObject(rawQuestion)?.options)
      && (optionalObject(rawQuestion)?.options as unknown[]).some((raw) =>
        stringValue(optionalObject(raw)?.option_id) === "tell_agent_differently"
      )
    ),
  };
}

function customerInteraction(value: unknown):
  | { kind: "message"; text: string }
  | { kind: "input"; input: CustomerInputRequest }
  | null {
  const interaction = optionalObject(value);
  if (!interaction) return null;
  const schemaVersion = stringValue(interaction.schema_version);
  const responseText = stringValue(interaction.response_text);
  if (schemaVersion === "typed-interaction.v1" && responseText) {
    return { kind: "message", text: responseText };
  }
  if (
    schemaVersion !== "typed-topic-choice.v1"
    || !responseText
    || !Array.isArray(interaction.options)
  ) return null;
  const recommended = stringValue(interaction.recommended_topic_id);
  const options = interaction.options.flatMap((raw) => {
    const option = optionalObject(raw);
    const optionKey = stringValue(option?.topic_id);
    const label = stringValue(option?.label);
    const description = stringValue(option?.description);
    if (!optionKey || !label || !description) return [];
    return [{ optionKey, label, description, recommended: optionKey === recommended }];
  });
  if (options.length < 2 || options.length > 3) return null;
  return {
    kind: "input",
    input: {
      kind: "topic_choice",
      title: "请选择要继续的主题",
      question: responseText,
      explanation: "",
      options,
      questions: [{
        questionKey: "topic_choice",
        question: responseText,
        explanation: "",
        options,
      }],
      allowFreeform: interaction.allow_free_text === true,
    },
  };
}

function transportFrom(
  source: CustomerSnapshotSource,
  runHandle: string | null,
  actionHandle: string | null,
): CustomerAnalysisTransport {
  const operationalFailure = optionalObject(source.run?.request.operational_failure);
  return {
    threadHandle: source.thread.id,
    runHandle,
    actionHandle,
    actionKind: actionHandle
      ? source.agentHead?.status === "needs_input"
        ? "agent_pending_action"
        : source.run?.status === "interaction_completed"
          ? "topic_choice"
          : "bi_clarification"
      : null,
    eventsUrl: `/api/threads/${encodeURIComponent(source.thread.id)}/events`,
    eventCursor: source.eventCursor ?? source.stateVersion,
    latestItemSequence: source.latestItemSequence ?? source.messages.length,
    messageHistory: source.messageHistory ?? {
      hasMore: false,
      beforeCursor: null,
    },
    acceptedOperationIds: [...new Set(source.acceptedOperationIds)],
    technicalDetailRef: stringValue(operationalFailure?.failure_ref) || null,
  };
}

function stateFromAgentHead(source: CustomerSnapshotSource): {
  state: CustomerAnalysisState;
  runHandle: string | null;
  actionHandle: string | null;
  ownedMessageKey: string | null;
} | null {
  const head = source.agentHead;
  if (!head) return null;
  const terminal = source.agentTerminal;
  const terminalAssistant = terminal
    ? source.messages.find((message) =>
        message.role === "assistant"
        && message.itemType === "assistant_message"
        && message.operationKey === `assistant:${terminal.operationId}`
      )
    : undefined;
  if (
    (head.status === "idle" || head.status === "failed")
    && terminal?.status === "failed"
  ) {
    if (!terminalAssistant || terminal.completionKind !== "failed_turn") {
      throw new Error("customer_agent_terminal_message_missing");
    }
    return {
      state: {
        status: "failed",
        phase: "understanding",
        title: "当前请求未完成",
        description: terminalAssistant.text,
        updates: [{
          key: "understanding",
          text: "请求未完成，可以继续输入",
          status: "failed",
          confirmedAt: source.confirmedAt,
        }],
        recovery: "retry",
      },
      runHandle: null,
      actionHandle: null,
      ownedMessageKey: terminalAssistant.key,
    };
  }
  if (head.status === "idle") return null;
  if (
    (head.status === "completed" || head.status === "completed_with_limits")
    && terminal
    && terminal.status === head.status
  ) {
    if (!terminalAssistant || terminal.completionKind === "failed_turn") {
      throw new Error("customer_agent_terminal_message_missing");
    }
    const finalOutput = terminal.finalOutput;
    const materialRefs = stringArray(finalOutput?.materialRefs);
    const limitationRefs = stringArray(finalOutput?.limitationRefs);
    const analysisPublication = terminal.completionKind === "analysis_publication";
    if (analysisPublication && (
      !terminal.durableTaskRef
      || terminal.durableTaskRef !== source.customerPublicationTaskRef
      || !source.customerPublication
    )) {
      throw new Error("customer_agent_publication_binding_invalid");
    }
    const answer = analysisPublication
      ? answerFromPublication(source.customerPublication as CustomerPublication)
      : {
          blocks: [{
            key: "agent-response",
            kind: "summary" as const,
            heading: null,
            text: terminalAssistant.text,
          }],
          warnings: limitationRefs,
          evidenceCount: materialRefs.length,
          limitationCount: limitationRefs.length,
        };
    return {
      state: {
        status: head.status,
        phase: "delivering",
        title: analysisPublication
          ? head.status === "completed_with_limits"
            ? "业务参考已生成，结论有适用边界"
            : "业务参考已生成"
          : head.status === "completed_with_limits"
            ? "回复已生成，内容有适用边界"
            : "回复已生成",
        description: head.status === "completed_with_limits"
          ? "请结合回复中列出的材料和限制进行业务判断。"
          : "当前回复已持久化，可继续追问。",
        updates: [{
          key: "delivering",
          text: "已生成回复",
          status: "completed",
          confirmedAt: source.confirmedAt,
        }],
        answer,
      },
      runHandle: null,
      actionHandle: null,
      ownedMessageKey: terminalAssistant.key,
    };
  }
  if (head.status === "needs_input") {
    const input = inputFromPendingAction(source.pendingAction);
    if (!input || !head.pendingActionRef) {
      throw new Error("customer_pending_action_missing");
    }
    return {
      state: {
        status: "needs_input",
        phase: "understanding",
        title: "需要你的确认",
        description: "提交后会从当前持久化 checkpoint 继续。",
        updates: [{
          key: "understanding",
          text: "等待确认",
          status: "active",
          confirmedAt: source.confirmedAt,
        }],
        input,
      },
      runHandle: null,
      actionHandle: head.pendingActionRef,
      ownedMessageKey: null,
    };
  }
  if (
    head.status === "working"
    && (!source.run || head.activeTaskRef !== source.run.id)
  ) {
    return {
      state: {
        status: "working",
        phase: "understanding",
        title: "正在处理当前请求",
        description: "可以关闭页面，进度和最终回复会从持久化状态恢复。",
        updates: [{
          key: "understanding",
          text: "处理请求",
          status: "active",
          confirmedAt: source.confirmedAt,
        }],
        safeToClose: true,
      },
      runHandle: head.activeTaskRef,
      actionHandle: null,
      ownedMessageKey: null,
    };
  }
  return null;
}

const STATE_OWNED_ITEM_TYPES = new Set([
  "progress",
  "clarification",
  "approval_request",
]);

function customerConversationMessages(
  messages: CustomerSnapshotMessage[],
  ownedMessageKey: string | null,
): CustomerMessage[] {
  return messages.flatMap((message) => {
    if (
      message.key === ownedMessageKey
      || (message.itemType && STATE_OWNED_ITEM_TYPES.has(message.itemType))
    ) return [];
    if (message.itemType === "clarification_resolution") {
      return [{
        key: message.key,
        role: "assistant" as const,
        text: clarificationResolutionText(message.text),
        createdAt: message.createdAt,
      }];
    }
    return [{
      key: message.key,
      role: message.role,
      text: message.text,
      createdAt: message.createdAt,
    }];
  });
}

function clarificationResolutionText(value: string) {
  const answer = value.trim().replace(/[。；]+$/u, "");
  return `我会按以下口径继续分析：${answer}。`;
}

function inputFromPendingAction(
  value: Record<string, unknown> | null | undefined,
): CustomerInputRequest | null {
  if (!value) return null;
  const actionRef = stringValue(value.actionRef);
  const actionType = stringValue(value.actionType);
  const prompt = stringValue(value.prompt);
  if (!actionRef || !prompt) return null;
  if (actionType === "ask_user" && Array.isArray(value.options)) {
    const options = value.options.flatMap((raw) => {
      const item = optionalObject(raw);
      const optionKey = stringValue(item?.optionId);
      const label = stringValue(item?.label);
      const description = stringValue(item?.description);
      if (!optionKey || !label || !description) return [];
      return [{ optionKey, label, description, recommended: item?.recommended === true }];
    });
    if (options.length < 2 || options.length > 3) return null;
    return {
      kind: "clarification",
      title: "需要确认后继续",
      question: prompt,
      explanation: "",
      options,
      questions: [{
        questionKey: actionRef,
        question: prompt,
        explanation: "",
        options,
      }],
      allowFreeform: true,
    };
  }
  if (actionType === "request_approval") {
    const options = [
      { optionKey: "approved", label: "批准", description: "允许执行所述操作。", recommended: false },
      { optionKey: "rejected", label: "拒绝", description: "不执行所述操作。", recommended: false },
    ];
    return {
      kind: "clarification",
      title: "需要批准后继续",
      question: prompt,
      explanation: stringValue(value.sideEffectScope),
      options,
      questions: [{
        questionKey: actionRef,
        question: prompt,
        explanation: stringValue(value.sideEffectScope),
        options,
      }],
      allowFreeform: false,
    };
  }
  return null;
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.length > 0);
}

function conversationTitle(messages: CustomerMessage[]) {
  const firstQuestion = messages.find((message) => message.role === "user")?.text.trim();
  if (!firstQuestion) return "新分析";
  return firstQuestion.length > 34 ? `${firstQuestion.slice(0, 34)}…` : firstQuestion;
}

function validateState(value: unknown): asserts value is CustomerAnalysisState {
  const state = requiredObject(value, "customer_snapshot_invalid");
  if (!isMainStatus(state.status)) throw new Error("customer_snapshot_invalid");
  requiredString(state.title, "customer_snapshot_invalid");
  requiredString(state.description, "customer_snapshot_invalid");
  if (!Array.isArray(state.updates)) {
    throw new Error("customer_snapshot_invalid");
  }
  state.updates.forEach((update) => {
    const item = requiredObject(update, "customer_snapshot_invalid");
    assertExactKeys(
      item,
      ["key", "text", "status", "confirmedAt"],
      "customer_snapshot_invalid",
    );
    if (
      !CUSTOMER_PHASES.some((phase) => phase.id === item.key)
      || !["completed", "active", "failed"].includes(String(item.status))
    ) throw new Error("customer_snapshot_invalid");
    requiredString(item.text, "customer_snapshot_invalid");
    requiredTimestamp(item.confirmedAt, "customer_snapshot_invalid");
  });
  const expectedKeys = state.status === "idle"
    ? ["status", "title", "description", "updates"]
    : state.status === "working" || state.status === "checkpoint"
      ? ["status", "title", "description", "updates", "phase", "safeToClose"]
      : state.status === "needs_input"
        ? ["status", "title", "description", "updates", "phase", "input"]
        : state.status === "completed" || state.status === "completed_with_limits"
          ? ["status", "title", "description", "updates", "phase", "answer"]
          : ["status", "title", "description", "updates", "phase", "recovery"];
  assertExactKeys(state, expectedKeys, "customer_snapshot_invalid");
  if (state.status !== "idle") {
    if (!CUSTOMER_PHASES.some((phase) => phase.id === state.phase)) {
      throw new Error("customer_snapshot_invalid");
    }
  }
  if (
    (state.status === "working" || state.status === "checkpoint")
    && state.safeToClose !== true
  ) {
    throw new Error("customer_snapshot_invalid");
  }
  if (state.status === "needs_input") validateInput(state.input);
  if (state.status === "completed" || state.status === "completed_with_limits") {
    validateAnswer(state.answer);
  }
  if (state.status === "failed"
    && !["retry", "new_analysis", "contact_support"].includes(String(state.recovery))) {
    throw new Error("customer_snapshot_invalid");
  }
}

function validateInput(value: unknown) {
  const input = requiredObject(value, "customer_snapshot_invalid");
  assertExactKeys(input, [
    "kind",
    "title",
    "question",
    "explanation",
    "options",
    "questions",
    "allowFreeform",
  ], "customer_snapshot_invalid");
  if (input.kind !== "clarification" && input.kind !== "topic_choice") {
    throw new Error("customer_snapshot_invalid");
  }
  requiredString(input.title, "customer_snapshot_invalid");
  requiredString(input.question, "customer_snapshot_invalid");
  if (!Array.isArray(input.options) || input.options.length < 2 || input.options.length > 3) {
    throw new Error("customer_snapshot_invalid");
  }
  input.options.forEach((option) => {
    const item = requiredObject(option, "customer_snapshot_invalid");
    assertExactKeys(item, [
      "optionKey",
      "label",
      "description",
      "recommended",
    ], "customer_snapshot_invalid");
    requiredString(item.optionKey, "customer_snapshot_invalid");
    requiredString(item.label, "customer_snapshot_invalid");
    requiredString(item.description, "customer_snapshot_invalid");
    if (typeof item.recommended !== "boolean") throw new Error("customer_snapshot_invalid");
  });
  if (!Array.isArray(input.questions) || !input.questions.length) {
    throw new Error("customer_snapshot_invalid");
  }
  input.questions.forEach((question) => {
    const item = requiredObject(question, "customer_snapshot_invalid");
    assertExactKeys(item, [
      "questionKey",
      "question",
      "explanation",
      "options",
    ], "customer_snapshot_invalid");
    requiredString(item.questionKey, "customer_snapshot_invalid");
    requiredString(item.question, "customer_snapshot_invalid");
    if (typeof item.explanation !== "string") {
      throw new Error("customer_snapshot_invalid");
    }
    if (!Array.isArray(item.options) || item.options.length < 2 || item.options.length > 3) {
      throw new Error("customer_snapshot_invalid");
    }
    item.options.forEach((option) => {
      const candidate = requiredObject(option, "customer_snapshot_invalid");
      assertExactKeys(candidate, [
        "optionKey",
        "label",
        "description",
        "recommended",
      ], "customer_snapshot_invalid");
      requiredString(candidate.optionKey, "customer_snapshot_invalid");
      requiredString(candidate.label, "customer_snapshot_invalid");
      requiredString(candidate.description, "customer_snapshot_invalid");
      if (typeof candidate.recommended !== "boolean") {
        throw new Error("customer_snapshot_invalid");
      }
    });
  });
  if (typeof input.allowFreeform !== "boolean") throw new Error("customer_snapshot_invalid");
}

function validateAnswer(value: unknown) {
  const answer = requiredObject(value, "customer_snapshot_invalid");
  assertExactKeys(answer, [
    "blocks",
    "warnings",
    "evidenceCount",
    "limitationCount",
  ], "customer_snapshot_invalid");
  if (!Array.isArray(answer.blocks) || !Number.isInteger(answer.evidenceCount)
    || !Number.isInteger(answer.limitationCount)
    || !Array.isArray(answer.warnings)) {
    throw new Error("customer_snapshot_invalid");
  }
  answer.blocks.forEach((block) => {
    const item = requiredObject(block, "customer_snapshot_invalid");
    assertExactKeys(item, ["key", "kind", "heading", "text"], "customer_snapshot_invalid");
    requiredString(item.key, "customer_snapshot_invalid");
    requiredString(item.text, "customer_snapshot_invalid");
    if (item.heading !== null) requiredString(item.heading, "customer_snapshot_invalid");
    if (!["summary", "finding", "context", "limitation", "recommendation"].includes(
      String(item.kind),
    )) throw new Error("customer_snapshot_invalid");
  });
  answer.warnings.forEach((warning) => requiredString(
    warning,
    "customer_snapshot_invalid",
  ));
}

function validateTransport(value: unknown) {
  const transport = requiredObject(value, "customer_snapshot_invalid");
  assertExactKeys(transport, [
    "threadHandle",
    "runHandle",
    "actionHandle",
    "actionKind",
    "eventsUrl",
    "eventCursor",
    "latestItemSequence",
    "messageHistory",
    "acceptedOperationIds",
    "technicalDetailRef",
  ], "customer_snapshot_invalid");
  requiredString(transport.threadHandle, "customer_snapshot_invalid");
  requiredString(transport.eventCursor, "customer_snapshot_invalid");
  if (transport.actionKind !== null && ![
    "agent_pending_action",
    "bi_clarification",
    "topic_choice",
  ].includes(String(transport.actionKind))) {
    throw new Error("customer_snapshot_invalid");
  }
  if (!Number.isInteger(transport.latestItemSequence)
    || Number(transport.latestItemSequence) < 0) {
    throw new Error("customer_snapshot_invalid");
  }
  const messageHistory = requiredObject(
    transport.messageHistory,
    "customer_snapshot_invalid",
  );
  assertExactKeys(
    messageHistory,
    ["hasMore", "beforeCursor"],
    "customer_snapshot_invalid",
  );
  if (
    typeof messageHistory.hasMore !== "boolean"
    || (messageHistory.beforeCursor !== null
      && (typeof messageHistory.beforeCursor !== "string"
        || !messageHistory.beforeCursor))
  ) throw new Error("customer_snapshot_invalid");
  if (!Array.isArray(transport.acceptedOperationIds)
    || transport.acceptedOperationIds.some((item) => typeof item !== "string" || !item)) {
    throw new Error("customer_snapshot_invalid");
  }
}

function assertExactKeys(
  value: Record<string, unknown>,
  expected: string[],
  code: string,
) {
  const actual = Object.keys(value).sort();
  const sortedExpected = [...expected].sort();
  if (
    actual.length !== sortedExpected.length
    || actual.some((key, index) => key !== sortedExpected[index])
  ) throw new Error(code);
}

function isMainStatus(value: unknown): value is CustomerMainStatus {
  return [
    "idle",
    "working",
    "needs_input",
    "checkpoint",
    "completed",
    "completed_with_limits",
    "failed",
  ].includes(String(value));
}

function requiredObject(value: unknown, code: string): Record<string, any> {
  const object = optionalObject(value);
  if (!object) throw new Error(code);
  return object;
}

function optionalObject(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function requiredString(value: unknown, code: string): string {
  const result = stringValue(value);
  if (!result) throw new Error(code);
  return result;
}

function requiredTimestamp(value: unknown, code: string): string {
  const result = requiredString(value, code);
  if (Number.isNaN(Date.parse(result))) throw new Error(code);
  return result;
}

function stringValue(value: unknown) {
  return typeof value === "string" ? value.trim() : "";
}
