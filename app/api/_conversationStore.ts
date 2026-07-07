import { Pool } from "pg";

type ThreadRecord = {
  id: string;
  ownerId: string;
  topicIds: string[];
  messages: MessageRecord[];
  createdAt: string;
};

type MessageRecord = {
  id: string;
  role: "user" | "assistant" | "system";
  text: string;
  createdAt: string;
};

type RunRecord = {
  id: string;
  threadId: string;
  status: "queued" | "running" | "completed" | "waiting_for_clarification";
  createdAt: string;
};

type MemoryProposalRecord = {
  id: string;
  threadId: string;
  text: string;
  status: "proposed" | "accepted" | "rejected";
  createdAt: string;
};

type ArtifactRecord = {
  id: string;
  threadId: string;
  topicId: string;
  snapshotId: string;
  permissionScope: string;
  followUpContext: string;
  createdAt: string;
};

type VisibleArtifactRecord = ArtifactRecord & {
  visibleSectionIds: string[];
  hiddenSectionCount: number;
  answerPackage: Record<string, unknown>;
};

type RunEvent = {
  event: string;
  runId: string;
  threadId?: string;
  payload?: unknown;
  process?: RunProcessEvent;
};

type RunProcessEvent = {
  stage: string;
  label: string;
  summary: string;
  status?: string;
};

type PersistedAnswerPackageRun = {
  runId: string;
  threadId: string;
  runStatus: string;
  question: string;
  answerPackage: Record<string, unknown>;
  createdAt: string;
};

export type PersistedRuntimeRun = {
  runId: string;
  threadId: string;
  runStatus: string;
  question: string;
  request: Record<string, unknown>;
  createdAt: string;
};

type MemoryStore = {
  threads: Map<string, ThreadRecord>;
  runs: Map<string, RunRecord>;
  artifacts: Map<string, ArtifactRecord>;
  memoryProposals: Map<string, MemoryProposalRecord>;
};

const globalStore = globalThis as typeof globalThis & {
  __wajeConversationMemoryStore?: MemoryStore;
  __wajeConversationPool?: Pool;
};

export function conversationStoreMode() {
  const databaseUrl = process.env.WAJE_RUNTIME_DATABASE_URL || process.env.DATABASE_URL;
  if (databaseUrl) return "postgres";
  if (process.env.NODE_ENV === "production") {
    throw new Error("WAJE_RUNTIME_DATABASE_URL or DATABASE_URL is required");
  }
  return "memory";
}

export async function listThreads(): Promise<ThreadRecord[]> {
  if (conversationStoreMode() === "postgres") {
    const { rows } = await pool().query(`
      SELECT thread_id, owner_id, created_at
      FROM waje_runtime.investigation_threads
      ORDER BY created_at DESC
    `);
    return rows.map((row) => ({
      id: row.thread_id,
      ownerId: row.owner_id,
      topicIds: [],
      messages: [],
      createdAt: row.created_at,
    }));
  }
  return [...memoryStore().threads.values()];
}

export async function createThread(ownerId = "local-user"): Promise<ThreadRecord> {
  if (conversationStoreMode() === "postgres") {
    const thread: ThreadRecord = {
      id: `thread-${crypto.randomUUID()}`,
      ownerId,
      topicIds: [],
      messages: [],
      createdAt: new Date().toISOString(),
    };
    await pool().query(
      `
      INSERT INTO waje_runtime.investigation_threads(thread_id, owner_id)
      VALUES ($1, $2)
      `,
      [thread.id, ownerId],
    );
    await audit("thread_created", { threadId: thread.id, ref: thread.id, payload: { ownerId } });
    return thread;
  }
  const store = memoryStore();
  const thread: ThreadRecord = {
    id: `thread-${crypto.randomUUID()}`,
    ownerId,
    topicIds: [],
    messages: [],
    createdAt: new Date().toISOString(),
  };
  store.threads.set(thread.id, thread);
  return thread;
}

export async function requireThread(threadId: string): Promise<ThreadRecord> {
  if (conversationStoreMode() === "postgres") {
    const { rows } = await pool().query(
      `
      SELECT thread_id, owner_id, created_at
      FROM waje_runtime.investigation_threads
      WHERE thread_id = $1
      `,
      [threadId],
    );
    const row = rows[0];
    if (!row) throw new Error("thread_not_found");
    return {
      id: row.thread_id,
      ownerId: row.owner_id,
      topicIds: [],
      messages: [],
      createdAt: row.created_at,
    } satisfies ThreadRecord;
  }
  const thread = memoryStore().threads.get(threadId);
  if (!thread) throw new Error("thread_not_found");
  return thread;
}

export async function createRun(threadId: string): Promise<RunRecord> {
  if (conversationStoreMode() === "postgres") {
    const run: RunRecord = {
      id: `run-${crypto.randomUUID()}`,
      threadId,
      status: "queued",
      createdAt: new Date().toISOString(),
    };
    await pool().query(
      `
      INSERT INTO waje_runtime.analysis_runs(run_id, thread_id, status)
      VALUES ($1, $2, $3)
      `,
      [run.id, threadId, run.status],
    );
    await audit("run_queued", { threadId, runId: run.id, ref: run.id });
    return run;
  }
  const run: RunRecord = {
    id: `run-${crypto.randomUUID()}`,
    threadId,
    status: "queued",
    createdAt: new Date().toISOString(),
  };
  memoryStore().runs.set(run.id, run);
  return run;
}

export async function requireRun(runId: string): Promise<RunRecord> {
  if (conversationStoreMode() === "postgres") {
    const { rows } = await pool().query(
      `
      SELECT run_id, thread_id, status, created_at
      FROM waje_runtime.analysis_runs
      WHERE run_id = $1
      `,
      [runId],
    );
    const row = rows[0];
    if (!row) throw new Error("run_not_found");
    return {
      id: row.run_id,
      threadId: row.thread_id,
      status: row.status,
      createdAt: row.created_at,
    } satisfies RunRecord;
  }
  const run = memoryStore().runs.get(runId);
  if (!run) throw new Error("run_not_found");
  return run;
}

export async function runEvents(runId: string): Promise<RunEvent[]> {
  const run = await requireRun(runId);
  if (conversationStoreMode() === "postgres") {
    const events: RunEvent[] = [
      {
        event: "run_status",
        runId,
        threadId: run.threadId,
        payload: { status: run.status },
        process: processEvent("run_status", { status: run.status }),
      },
    ];
    const auditRows = await pool().query(
      `
      SELECT event_type, payload, created_at
      FROM waje_runtime.audit_events
      WHERE run_id = $1 OR ref = $1
      ORDER BY created_at
      `,
      [runId],
    );
    for (const row of auditRows.rows) {
      events.push({
        event: row.event_type,
        runId,
        threadId: run.threadId,
        payload: row.payload,
        process: processEvent(row.event_type, row.payload),
      });
    }
    const nodeRows = await pool().query(
      `
      SELECT node_name, status, payload, started_at, finished_at
      FROM waje_runtime.run_nodes
      WHERE run_id = $1
      ORDER BY started_at NULLS LAST, finished_at NULLS LAST, node_id
      `,
      [runId],
    );
    for (const row of nodeRows.rows) {
      const payload = {
        node_name: row.node_name,
        status: row.status,
        payload: row.payload,
        started_at: row.started_at,
        finished_at: row.finished_at,
      };
      events.push({
        event: "node_process",
        runId,
        threadId: run.threadId,
        payload,
        process: processNodeEvent(row.node_name, row.status, row.payload),
      });
    }
    const packageRows = await pool().query(
      `
      SELECT status, payload
      FROM waje_runtime.answer_packages
      WHERE run_id = $1
      ORDER BY created_at DESC
      LIMIT 1
      `,
      [runId],
    );
    if (packageRows.rows[0]) {
      events.push({
        event: "answer_package_ready",
        runId,
        threadId: run.threadId,
        payload: {
          status: packageRows.rows[0].status,
          answer_package: packageRows.rows[0].payload,
        },
        process: processEvent("answer_package_ready", { status: packageRows.rows[0].status }),
      });
    }
    return events;
  }
  return [
    {
      event: "run_status",
      runId,
      threadId: run.threadId,
      payload: { status: run.status },
      process: processEvent("run_status", { status: run.status }),
    },
  ];
}

export async function listPersistedAnswerPackageRuns(limit = 20): Promise<PersistedAnswerPackageRun[]> {
  if (conversationStoreMode() !== "postgres") return [];
  const { rows } = await pool().query(
    `
    SELECT r.run_id, r.thread_id, r.status AS run_status, r.request, p.payload, p.created_at
    FROM waje_runtime.answer_packages p
    JOIN waje_runtime.analysis_runs r ON r.run_id = p.run_id
    ORDER BY p.created_at DESC
    LIMIT $1
    `,
    [limit],
  );
  return rows.map((row) => {
    const request = row.request ?? {};
    return {
      runId: row.run_id,
      threadId: row.thread_id,
      runStatus: row.run_status,
      question: String(request.question ?? request.user_message ?? ""),
      answerPackage: row.payload,
      createdAt: row.created_at,
    } satisfies PersistedAnswerPackageRun;
  });
}

export async function listPersistedRuntimeRuns(limit = 20): Promise<PersistedRuntimeRun[]> {
  if (conversationStoreMode() !== "postgres") return [];
  const { rows } = await pool().query(
    `
    SELECT r.run_id, r.thread_id, r.status AS run_status, r.request, r.created_at
    FROM waje_runtime.analysis_runs r
    WHERE NOT EXISTS (
      SELECT 1
      FROM waje_runtime.answer_packages p
      WHERE p.run_id = r.run_id
    )
    ORDER BY r.created_at DESC
    LIMIT $1
    `,
    [limit],
  );
  return rows.map((row) => {
    const request = row.request ?? {};
    return {
      runId: row.run_id,
      threadId: row.thread_id,
      runStatus: row.run_status,
      question: String(request.question ?? request.user_message ?? ""),
      request,
      createdAt: row.created_at,
    } satisfies PersistedRuntimeRun;
  });
}

export async function addUserMessage(threadId: string, text: string): Promise<MessageRecord> {
  if (conversationStoreMode() === "postgres") {
    await requireThread(threadId);
    const message: MessageRecord = {
      id: `message-${crypto.randomUUID()}`,
      role: "user",
      text,
      createdAt: new Date().toISOString(),
    };
    await pool().query(
      `
      INSERT INTO waje_runtime.conversation_messages(message_id, thread_id, role, text)
      VALUES ($1, $2, $3, $4)
      `,
      [message.id, threadId, message.role, text],
    );
    await audit("message_recorded", { threadId, ref: message.id });
    return message;
  }
  const thread = await requireThread(threadId);
  const message: MessageRecord = {
    id: `message-${crypto.randomUUID()}`,
    role: "user",
    text,
    createdAt: new Date().toISOString(),
  };
  thread.messages.push(message);
  return message;
}

export async function requireArtifactForContinue(
  artifactId: string,
  role = "analyst",
): Promise<ArtifactRecord> {
  if (conversationStoreMode() === "postgres") {
    const { rows } = await pool().query(
      `
      SELECT artifact_id, thread_id, topic_id, snapshot_id, permission_scope, follow_up_context, created_at
      FROM waje_runtime.investigation_artifacts
      WHERE artifact_id = $1
      `,
      [artifactId],
    );
    const row = rows[0];
    if (!row) throw new Error("artifact_not_found");
    if (!canReadScope(role, row.permission_scope)) {
      await audit("artifact_continue_blocked", {
        threadId: row.thread_id,
        topicId: row.topic_id,
        ref: artifactId,
        payload: { role, permission_scope: row.permission_scope },
      });
      throw new Error("artifact_permission_denied");
    }
    await audit("artifact_continue_allowed", {
      threadId: row.thread_id,
      topicId: row.topic_id,
      ref: artifactId,
      payload: { role, permission_scope: row.permission_scope },
    });
    return {
      id: row.artifact_id,
      threadId: row.thread_id,
      topicId: row.topic_id,
      snapshotId: row.snapshot_id,
      permissionScope: row.permission_scope,
      followUpContext: row.follow_up_context,
      createdAt: row.created_at,
    } satisfies ArtifactRecord;
  }
  const artifact = memoryStore().artifacts.get(artifactId);
  if (!artifact) throw new Error("artifact_not_found");
  if (!canReadScope(role, artifact.permissionScope)) {
    throw new Error("artifact_permission_denied");
  }
  return artifact;
}

export async function readArtifactForRole(
  artifactId: string,
  role = "analyst",
  action: "open" | "export" = "open",
): Promise<VisibleArtifactRecord> {
  if (conversationStoreMode() === "postgres") {
    const { rows } = await pool().query(
      `
      SELECT
        a.artifact_id,
        a.thread_id,
        a.topic_id,
        a.snapshot_id,
        a.permission_scope,
        a.follow_up_context,
        a.created_at,
        COALESCE(NULLIF(a.payload, '{}'::jsonb), p.payload, '{}'::jsonb) AS answer_package
      FROM waje_runtime.investigation_artifacts a
      LEFT JOIN LATERAL (
        SELECT payload
        FROM waje_runtime.answer_packages
        WHERE artifact_id = a.artifact_id OR run_id = a.run_id
        ORDER BY created_at DESC
        LIMIT 1
      ) p ON true
      WHERE a.artifact_id = $1
      `,
      [artifactId],
    );
    const row = rows[0];
    if (!row) throw new Error("artifact_not_found");
    if (!canReadScope(role, row.permission_scope)) {
      await audit(`artifact_${action}_blocked`, {
        threadId: row.thread_id,
        topicId: row.topic_id,
        ref: artifactId,
        payload: { role, permission_scope: row.permission_scope },
      });
      throw new Error("artifact_permission_denied");
    }
    const answerPackage = filterAnswerPackageForRole(row.answer_package ?? {}, role);
    const visibleSectionIds = visibleSections(answerPackage);
    await audit(action === "export" ? "artifact_exported" : "artifact_opened", {
      threadId: row.thread_id,
      topicId: row.topic_id,
      ref: artifactId,
      payload: { role, visibleSectionIds },
    });
    return {
      id: row.artifact_id,
      threadId: row.thread_id,
      topicId: row.topic_id,
      snapshotId: row.snapshot_id,
      permissionScope: row.permission_scope,
      followUpContext: row.follow_up_context,
      createdAt: row.created_at,
      visibleSectionIds,
      hiddenSectionCount: hiddenSectionCount(row.answer_package ?? {}, answerPackage),
      answerPackage,
    } satisfies VisibleArtifactRecord;
  }
  const artifact = await requireArtifactForContinue(artifactId, role);
  return {
    ...artifact,
    visibleSectionIds: [],
    hiddenSectionCount: 0,
    answerPackage: {},
  };
}

export async function recordClarificationOutcome(runId: string, answer: string) {
  const run = await requireRun(runId);
  if (conversationStoreMode() === "postgres") {
    await audit("clarification_answer_recorded", {
      threadId: run.threadId,
      runId,
      ref: runId,
      payload: { answer },
    });
  }
  return { runId, threadId: run.threadId, answer, status: "accepted" };
}

export async function createMemoryProposal(threadId: string, text: string): Promise<MemoryProposalRecord> {
  if (conversationStoreMode() === "postgres") {
    const proposal: MemoryProposalRecord = {
      id: `memory-proposal-${crypto.randomUUID()}`,
      threadId,
      text,
      status: "proposed",
      createdAt: new Date().toISOString(),
    };
    await pool().query(
      `
      INSERT INTO waje_runtime.memory_proposals(
        proposal_id, thread_id, text, source_ref, owner_scope, visibility, status
      )
      VALUES ($1, $2, $3, $4, $5, $6, $7)
      `,
      [proposal.id, threadId, text, proposal.id, "org-default", "analyst", proposal.status],
    );
    await audit("memory_proposal_recorded", { threadId, ref: proposal.id });
    return proposal;
  }
  const proposal: MemoryProposalRecord = {
    id: `memory-proposal-${crypto.randomUUID()}`,
    threadId,
    text,
    status: "proposed",
    createdAt: new Date().toISOString(),
  };
  memoryStore().memoryProposals.set(proposal.id, proposal);
  return proposal;
}

export async function updateMemoryProposal(
  proposalId: string,
  status: "accepted" | "rejected",
): Promise<MemoryProposalRecord> {
  if (conversationStoreMode() === "postgres") {
    const { rows } = await pool().query(
      `
      UPDATE waje_runtime.memory_proposals
      SET status = $2, decided_at = now()
      WHERE proposal_id = $1
      RETURNING proposal_id, thread_id, text, status, created_at
      `,
      [proposalId, status],
    );
    const row = rows[0];
    if (!row) throw new Error("memory_proposal_not_found");
    await audit(`memory_proposal_${status}`, { threadId: row.thread_id, ref: proposalId });
    return {
      id: row.proposal_id,
      threadId: row.thread_id,
      text: row.text,
      status: row.status,
      createdAt: row.created_at,
    } satisfies MemoryProposalRecord;
  }
  const proposal = memoryStore().memoryProposals.get(proposalId);
  if (!proposal) throw new Error("memory_proposal_not_found");
  proposal.status = status;
  return proposal;
}

export function jsonError(error: unknown, status = 404) {
  return Response.json(
    { error: error instanceof Error ? error.message : "unknown_error" },
    { status },
  );
}

function memoryStore() {
  globalStore.__wajeConversationMemoryStore ??= {
    threads: new Map(),
    runs: new Map(),
    artifacts: new Map(),
    memoryProposals: new Map(),
  };
  return globalStore.__wajeConversationMemoryStore;
}

function pool() {
  const connectionString = process.env.WAJE_RUNTIME_DATABASE_URL || process.env.DATABASE_URL;
  if (!connectionString) throw new Error("WAJE_RUNTIME_DATABASE_URL or DATABASE_URL is required");
  globalStore.__wajeConversationPool ??= new Pool({ connectionString });
  return globalStore.__wajeConversationPool;
}

async function audit(
  eventType: string,
  fields: { threadId?: string; topicId?: string; runId?: string; ref?: string; payload?: unknown },
) {
  await pool().query(
    `
    INSERT INTO waje_runtime.audit_events(event_type, thread_id, topic_id, run_id, ref, payload)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
    `,
    [
      eventType,
      fields.threadId ?? null,
      fields.topicId ?? null,
      fields.runId ?? null,
      fields.ref ?? null,
      JSON.stringify(fields.payload ?? {}),
    ],
  );
}

function canReadScope(role: string, permissionScope: string) {
  const rank: Record<string, number> = {
    business_reader: 1,
    analyst: 2,
    data_owner_admin: 3,
  };
  return (rank[role] ?? 0) >= (rank[permissionScope] ?? 3);
}

function filterAnswerPackageForRole(answerPackage: Record<string, unknown>, role: string) {
  if (role === "data_owner_admin") return answerPackage;
  const allowed = new Set(
    role === "analyst"
      ? ["business_summary", "aggregate_evidence", "diagnostic_detail"]
      : ["business_summary", "aggregate_evidence"],
  );
  const sections = Array.isArray(answerPackage.sections)
    ? answerPackage.sections.filter((section) => {
        if (!section || typeof section !== "object") return false;
        return allowed.has(String((section as Record<string, unknown>).visibility ?? ""));
      })
    : [];
  return {
    run_id: answerPackage.run_id,
    status: answerPackage.status,
    package_type: answerPackage.package_type,
    sections,
  };
}

function visibleSections(answerPackage: Record<string, unknown>) {
  if (!Array.isArray(answerPackage.sections)) return [];
  return answerPackage.sections.map((section) => {
    if (!section || typeof section !== "object") return "";
    return String((section as Record<string, unknown>).section_id ?? "");
  }).filter(Boolean);
}

function hiddenSectionCount(original: Record<string, unknown>, filtered: Record<string, unknown>) {
  const originalCount = Array.isArray(original.sections) ? original.sections.length : 0;
  const filteredCount = Array.isArray(filtered.sections) ? filtered.sections.length : 0;
  return Math.max(0, originalCount - filteredCount);
}

function processEvent(eventType: string, payload: unknown): RunProcessEvent {
  if (eventType === "clarification_requested") {
    const question = firstClarificationQuestion(payload);
    return {
      stage: "question",
      label: "需要用户确认",
      summary: question || "需要确认业务口径后继续执行。",
      status: "waiting_for_user",
    };
  }
  if (eventType === "answer_package_ready" || eventType === "answer_package_recorded") {
    return {
      stage: "answer",
      label: "答案已生成",
      summary: "已生成可审计的业务回答。",
      status: payloadStatus(payload),
    };
  }
  if (eventType === "workflow_failed") {
    return {
      stage: "block",
      label: "执行失败",
      summary: "执行链路失败，需要查看审计信息后重试或修复。",
      status: "failed",
    };
  }
  if (eventType === "context_manifest_recorded") {
    return {
      stage: "context",
      label: "上下文已锁定",
      summary: "本轮可用上下文和可支撑 claim 的范围已记录。",
    };
  }
  return {
    stage: "runtime",
    label: statusLabelForProcess(eventType, payload),
    summary: "运行状态已更新，详细审计保留在 payload。",
    status: payloadStatus(payload),
  };
}

function processNodeEvent(nodeName: string, status: string, payload: unknown): RunProcessEvent {
  if (["understand_business_intent", "decide_question_boundary", "confirm_business_understanding"].includes(nodeName)) {
    return {
      stage: "intent",
      label: "理解业务问题",
      summary: "已把用户输入绑定为本轮可执行的业务问题和边界。",
      status,
    };
  }
  if (["design_analysis_route", "accept_analysis_route"].includes(nodeName)) {
    return {
      stage: "accepted_plan",
      label: "分析路径已验收",
      summary: "已确认本轮要执行的证据路径和可接受分支。",
      status,
    };
  }
  if (["execute_capabilities", "reduce_evidence"].includes(nodeName)) {
    return {
      stage: "capability_progress",
      label: "证据路径推进",
      summary: capabilityProgressSummary(payload),
      status,
    };
  }
  if (["hard_verify_answer", "semantic_audit", "answer_verify"].includes(nodeName)) {
    return {
      stage: "verifier_result",
      label: "答案边界校验",
      summary: "已检查回答中的数字、证据引用、口径和表达强度。",
      status,
    };
  }
  if (["repair_analysis_route", "repair_answer", "generate_degraded_explanation", "generate_blocked_explanation"].includes(nodeName)) {
    return {
      stage: "repair_or_degrade",
      label: repairOrDegradeLabel(nodeName),
      summary: "已按证据和 verifier 结果调整回答路径或表达边界。",
      status,
    };
  }
  return {
    stage: "runtime",
    label: nodeName,
    summary: "运行节点已更新，详细审计保留在 payload。",
    status,
  };
}

function firstClarificationQuestion(payload: unknown) {
  if (!payload || typeof payload !== "object") return "";
  const questions = (payload as Record<string, unknown>).questions;
  if (!Array.isArray(questions)) return "";
  const first = questions[0];
  if (!first || typeof first !== "object") return "";
  return String((first as Record<string, unknown>).question ?? "");
}

function capabilityProgressSummary(payload: unknown) {
  if (!payload || typeof payload !== "object") return "已执行本轮所需证据路径。";
  const record = payload as Record<string, unknown>;
  const evidence = record.evidence ?? record.evidence_items ?? record.primary_evidence;
  if (Array.isArray(evidence) && evidence.length) return `已汇总 ${evidence.length} 条证据。`;
  return "已执行本轮所需证据路径。";
}

function repairOrDegradeLabel(nodeName: string) {
  if (nodeName === "generate_blocked_explanation") return "当前阻断";
  if (nodeName === "generate_degraded_explanation") return "给出有边界结论";
  return "修正分析过程";
}

function payloadStatus(payload: unknown) {
  if (!payload || typeof payload !== "object") return undefined;
  const status = (payload as Record<string, unknown>).status;
  return typeof status === "string" ? status : undefined;
}

function statusLabelForProcess(eventType: string, payload: unknown) {
  const status = payloadStatus(payload);
  if (status === "waiting_for_clarification") return "等待确认";
  if (status === "completed") return "运行完成";
  if (status === "running_workflow") return "正在执行";
  return eventType;
}
