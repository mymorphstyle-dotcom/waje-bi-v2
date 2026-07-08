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

type RunAuditTrace = {
  run: Record<string, unknown>;
  answerPackage: Record<string, unknown> | null;
  claims: unknown[];
  evidence: unknown[];
  verifier: unknown;
  runNodes: Record<string, unknown>[];
  evidenceRefs: Record<string, unknown>[];
  resultRefs: Record<string, unknown>[];
  auditEvents: Record<string, unknown>[];
  traceCompleteness: {
    hasAnswerPackage: boolean;
    hasVerifier: boolean;
    evidenceRefCount: number;
    resultRefCount: number;
    contractVersions: string[];
    snapshotIds: string[];
    queryRefs: string[];
  };
};

type RunRerunComparability = {
  baseRunId: string;
  candidateRunId: string;
  comparable: boolean;
  reasons: string[];
  base: RunAuditTrace["traceCompleteness"];
  candidate: RunAuditTrace["traceCompleteness"];
};

type LaunchDashboard = {
  slow_runs: Record<string, unknown>[];
  failed_runs: Record<string, unknown>[];
  degraded_runs: Record<string, unknown>[];
  blocked_runs: Record<string, unknown>[];
  verifier_failed_runs: Record<string, unknown>[];
  capability_error_runs: Record<string, unknown>[];
  compiler_block_runs: Record<string, unknown>[];
  permission_spike_runs: Record<string, unknown>[];
  contract_mismatch_runs: Record<string, unknown>[];
  ledger_mismatch_runs: Record<string, unknown>[];
};

type PersistedAnswerPackageRun = {
  runId: string;
  threadId: string;
  runStatus: string;
  question: string;
  answerPackage: Record<string, unknown>;
  runNodes: Record<string, unknown>[];
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

export async function runAuditTrace(runId: string): Promise<RunAuditTrace> {
  if (conversationStoreMode() !== "postgres") {
    const run = await requireRun(runId);
    return auditTracePayload({
      run: {
        run_id: run.id,
        thread_id: run.threadId,
        status: run.status,
        created_at: run.createdAt,
      },
    });
  }
  const runRows = await pool().query(
    `
    SELECT run_id, thread_id, turn_id, topic_id, status, request, created_at, updated_at
    FROM waje_runtime.analysis_runs
    WHERE run_id = $1
    `,
    [runId],
  );
  const run = runRows.rows[0];
  if (!run) throw new Error("run_not_found");

  const packageRows = await pool().query(
    `
    SELECT package_id, artifact_id, status, payload, created_at
    FROM waje_runtime.answer_packages
    WHERE run_id = $1
    ORDER BY created_at DESC
    LIMIT 1
    `,
    [runId],
  );
  const nodeRows = await pool().query(
    `
    SELECT node_id, node_name, status, payload, started_at, finished_at
    FROM waje_runtime.run_nodes
    WHERE run_id = $1
    ORDER BY started_at NULLS LAST, finished_at NULLS LAST, node_id
    `,
    [runId],
  );
  const evidenceRows = await pool().query(
    `
    SELECT
      e.evidence_ref,
      e.result_ref AS query_ref,
      e.payload,
      e.created_at,
      r.snapshot_id,
      r.contract_version,
      r.permission_scope,
      r.semantic_scope
    FROM waje_runtime.evidence_refs e
    LEFT JOIN waje_runtime.result_refs r ON r.result_ref = e.result_ref
    WHERE e.run_id = $1
    ORDER BY e.created_at, e.evidence_ref
    `,
    [runId],
  );
  const resultRows = run.topic_id
    ? await pool().query(
        `
        SELECT
          result_ref AS query_ref,
          snapshot_id,
          contract_version,
          permission_scope,
          semantic_scope,
          payload,
          created_at
        FROM waje_runtime.result_refs
        WHERE topic_id = $1
        ORDER BY created_at, result_ref
        `,
        [run.topic_id],
      )
    : { rows: [] };
  const auditRows = await pool().query(
    `
    SELECT audit_id, event_type, actor_id, thread_id, topic_id, run_id, ref, payload, created_at
    FROM waje_runtime.audit_events
    WHERE run_id = $1 OR ref = $1
    ORDER BY created_at, audit_id
    `,
    [runId],
  );

  const latestPackage = packageRows.rows[0];
  return auditTracePayload({
    run,
    answerPackage: latestPackage?.payload ?? null,
    claims: claimsFromPackage(latestPackage?.payload),
    evidence: evidenceFromPackage(latestPackage?.payload),
    verifier: verifierFromPackage(latestPackage?.payload),
    runNodes: nodeRows.rows,
    evidenceRefs: evidenceRows.rows,
    resultRefs: resultRows.rows,
    auditEvents: auditRows.rows,
  });
}

export async function runRerunComparability(baseRunId: string, candidateRunId: string): Promise<RunRerunComparability> {
  const baseTrace = await runAuditTrace(baseRunId);
  const candidateTrace = await runAuditTrace(candidateRunId);
  const base = baseTrace.traceCompleteness;
  const candidate = candidateTrace.traceCompleteness;
  const reasons: string[] = [];
  if (!sameSet(base.snapshotIds, candidate.snapshotIds)) reasons.push("snapshot_mismatch");
  if (!sameSet(base.contractVersions, candidate.contractVersions)) reasons.push("contract_version_mismatch");
  if (!sameSet(base.queryRefs, candidate.queryRefs)) reasons.push("query_ref_mismatch");
  return {
    baseRunId,
    candidateRunId,
    comparable: reasons.length === 0,
    reasons,
    base,
    candidate,
  };
}

export async function launchDashboard({ limit = 20, slowMs = 30000 } = {}): Promise<LaunchDashboard> {
  if (conversationStoreMode() !== "postgres") {
    return {
      slow_runs: [],
      failed_runs: [],
      degraded_runs: [],
      blocked_runs: [],
      verifier_failed_runs: [],
      capability_error_runs: [],
      compiler_block_runs: [],
      permission_spike_runs: [],
      contract_mismatch_runs: [],
      ledger_mismatch_runs: [],
    };
  }
  const slowRuns = await pool().query(
    `
    SELECT r.run_id, r.thread_id, r.status, max(EXTRACT(EPOCH FROM (n.finished_at - n.started_at)) * 1000)::bigint AS duration_ms
    FROM waje_runtime.analysis_runs r
    JOIN waje_runtime.run_nodes n ON n.run_id = r.run_id
    WHERE n.started_at IS NOT NULL AND n.finished_at IS NOT NULL
    GROUP BY r.run_id, r.thread_id, r.status
    HAVING max(EXTRACT(EPOCH FROM (n.finished_at - n.started_at)) * 1000) >= $1
    ORDER BY duration_ms DESC
    LIMIT $2
    `,
    [slowMs, limit],
  );
  const failedRuns = await pool().query(
    `
    SELECT r.run_id, r.thread_id, r.status, r.updated_at
    FROM waje_runtime.analysis_runs r
    WHERE r.status ILIKE '%failed%'
       OR EXISTS (
         SELECT 1 FROM waje_runtime.audit_events a
         WHERE (a.run_id = r.run_id OR a.ref = r.run_id) AND a.event_type = 'workflow_failed'
       )
    ORDER BY r.updated_at DESC
    LIMIT $1
    `,
    [limit],
  );
  const degradedRuns = await packageStatusRows("degraded", limit);
  const blockedRuns = await packageStatusRows("blocked", limit);
  const verifierFailedRuns = await pool().query(
    `
    SELECT
      r.run_id,
      r.thread_id,
      r.status,
      p.payload #>> '{admin_audit,verifier,status}' AS verifier_status,
      p.created_at
    FROM waje_runtime.analysis_runs r
    JOIN waje_runtime.answer_packages p ON p.run_id = r.run_id
    WHERE COALESCE(p.payload #>> '{admin_audit,verifier,status}', '') NOT IN ('', 'passed')
    ORDER BY p.created_at DESC
    LIMIT $1
    `,
    [limit],
  );
  const capabilityErrorRuns = await pool().query(
    `
    SELECT r.run_id, r.thread_id, n.node_name, n.status, n.payload, n.finished_at
    FROM waje_runtime.analysis_runs r
    JOIN waje_runtime.run_nodes n ON n.run_id = r.run_id
    WHERE (n.node_name = 'execute_capabilities' OR n.node_name ILIKE '%capability%')
      AND (
        n.status ILIKE '%failed%'
        OR n.status ILIKE '%blocked%'
        OR n.payload::text ILIKE '%capability_error%'
      )
    ORDER BY n.finished_at DESC NULLS LAST
    LIMIT $1
    `,
    [limit],
  );
  const compilerBlockRuns = await pool().query(
    `
    SELECT r.run_id, r.thread_id, n.node_name, n.status, n.payload, n.finished_at
    FROM waje_runtime.analysis_runs r
    JOIN waje_runtime.run_nodes n ON n.run_id = r.run_id
    WHERE n.node_name IN ('accept_analysis_route', 'repair_analysis_route')
      AND (
        n.status ILIKE '%blocked%'
        OR n.status ILIKE '%failed%'
        OR n.payload::text ILIKE '%compiler_block%'
      )
    ORDER BY n.finished_at DESC NULLS LAST
    LIMIT $1
    `,
    [limit],
  );
  const permissionSpikeRuns = await auditSignalRows(
    ["artifact_continue_blocked", "artifact_open_blocked", "artifact_export_blocked"],
    ["%permission_scope_mismatch%", "%permission_limited%"],
    limit,
  );
  const contractMismatchRuns = await auditSignalRows(
    [],
    ["%contract_version_mismatch%", "%contract_mismatch%"],
    limit,
  );
  const ledgerMismatchRuns = await auditSignalRows(
    [],
    ["%ledger_mismatch%", "%missing_contract%", "%unsupported_grain%"],
    limit,
  );

  return {
    slow_runs: slowRuns.rows,
    failed_runs: failedRuns.rows,
    degraded_runs: degradedRuns.rows,
    blocked_runs: blockedRuns.rows,
    verifier_failed_runs: verifierFailedRuns.rows,
    capability_error_runs: capabilityErrorRuns.rows,
    compiler_block_runs: compilerBlockRuns.rows,
    permission_spike_runs: permissionSpikeRuns.rows,
    contract_mismatch_runs: contractMismatchRuns.rows,
    ledger_mismatch_runs: ledgerMismatchRuns.rows,
  };
}

export async function listPersistedAnswerPackageRuns(limit = 20): Promise<PersistedAnswerPackageRun[]> {
  if (conversationStoreMode() !== "postgres") return [];
  const { rows } = await pool().query(
    `
    SELECT
      r.run_id,
      r.thread_id,
      r.status AS run_status,
      r.request,
      p.payload,
      p.created_at,
      COALESCE(nodes.run_nodes, '[]'::jsonb) AS run_nodes
    FROM waje_runtime.answer_packages p
    JOIN waje_runtime.analysis_runs r ON r.run_id = p.run_id
    LEFT JOIN LATERAL (
      SELECT jsonb_agg(payload ORDER BY node_id) AS run_nodes
      FROM waje_runtime.run_nodes
      WHERE run_id = r.run_id
    ) nodes ON true
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
      runNodes: Array.isArray(row.run_nodes) ? row.run_nodes : [],
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

export async function recordClarificationOutcome(
  runIdOrPayload: string | {
    runId: string;
    answer: string;
    selectedOptionId?: string | null;
    source?: string;
  },
  answerValue?: string,
) {
  const payload = typeof runIdOrPayload === "string"
    ? { runId: runIdOrPayload, answer: answerValue ?? "", selectedOptionId: null, source: "user" }
    : {
        runId: runIdOrPayload.runId,
        answer: runIdOrPayload.answer,
        selectedOptionId: runIdOrPayload.selectedOptionId ?? null,
        source: runIdOrPayload.source ?? "user",
      };
  const run = await requireRun(payload.runId);
  if (conversationStoreMode() === "postgres") {
    await audit("clarification_answer_recorded", {
      threadId: run.threadId,
      runId: payload.runId,
      ref: payload.runId,
      payload,
    });
  }
  return { ...payload, threadId: run.threadId, status: "accepted" };
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

async function packageStatusRows(status: "degraded" | "blocked", limit: number) {
  return pool().query(
    `
    SELECT
      r.run_id,
      r.thread_id,
      r.status AS run_status,
      p.status AS package_status,
      p.payload #>> '{final_explanation,status}' AS answer_status,
      p.created_at
    FROM waje_runtime.analysis_runs r
    JOIN waje_runtime.answer_packages p ON p.run_id = r.run_id
    WHERE p.status = $1 OR p.payload #>> '{final_explanation,status}' = $1
    ORDER BY p.created_at DESC
    LIMIT $2
    `,
    [status, limit],
  );
}

async function auditSignalRows(eventTypes: string[], payloadPatterns: string[], limit: number) {
  return pool().query(
    `
    SELECT audit_id, event_type, actor_id, thread_id, topic_id, run_id, ref, payload, created_at
    FROM waje_runtime.audit_events
    WHERE event_type = ANY($1::text[])
       OR payload::text ILIKE ANY($2::text[])
    ORDER BY created_at DESC, audit_id DESC
    LIMIT $3
    `,
    [eventTypes, payloadPatterns, limit],
  );
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
  const allowed = allowedVisibilitiesForRole(role);
  const sections = Array.isArray(answerPackage.sections)
    ? answerPackage.sections.filter((section) => {
        if (!section || typeof section !== "object") return false;
        return allowed.has(String((section as Record<string, unknown>).visibility ?? ""));
      }).map((section) => filterSectionPayloadForRole(section as Record<string, unknown>, allowed))
    : [];
  return {
    run_id: answerPackage.run_id,
    status: answerPackage.status,
    package_type: answerPackage.package_type,
    sections,
  };
}

function allowedVisibilitiesForRole(role: string) {
  return new Set(
    role === "analyst"
      ? ["business_summary", "aggregate_evidence", "diagnostic_detail"]
      : ["business_summary", "aggregate_evidence"],
  );
}

function filterSectionPayloadForRole(section: Record<string, unknown>, allowed: Set<string>) {
  if (section.section_id !== "summary") return section;
  const payload = section.payload;
  if (!payload || typeof payload !== "object") return section;
  return {
    ...section,
    payload: filterSummaryPayloadForRole(
      payload as Record<string, unknown>,
      allowed,
      String(section.visibility ?? ""),
    ),
  };
}

function filterSummaryPayloadForRole(
  payload: Record<string, unknown>,
  allowed: Set<string>,
  fallbackVisibility: string,
) {
  return {
    ...payload,
    claims: filterVisibleItems(payload.claims, allowed, fallbackVisibility),
    claim_groups: filterVisibleItems(payload.claim_groups, allowed, fallbackVisibility),
    visualization_plan: filterVisibleVisualizationPlan(
      payload.visualization_plan,
      allowed,
      fallbackVisibility,
    ),
  };
}

function filterVisibleItems(value: unknown, allowed: Set<string>, fallbackVisibility: string) {
  if (!Array.isArray(value)) return value;
  return value.filter((item) => {
    if (!item || typeof item !== "object") return false;
    return allowed.has(String((item as Record<string, unknown>).visibility ?? fallbackVisibility));
  });
}

function filterVisibleVisualizationPlan(value: unknown, allowed: Set<string>, fallbackVisibility: string) {
  if (!value || typeof value !== "object") return value;
  const plan = value as Record<string, unknown>;
  return {
    ...plan,
    blocks: filterVisibleItems(plan.blocks, allowed, fallbackVisibility),
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

function auditTracePayload({
  run,
  answerPackage = null,
  claims = [],
  evidence = [],
  verifier,
  runNodes = [],
  evidenceRefs = [],
  resultRefs = [],
  auditEvents = [],
}: Partial<RunAuditTrace> & { run: Record<string, unknown> }): RunAuditTrace {
  return {
    run,
    answerPackage,
    claims,
    evidence,
    verifier,
    runNodes,
    evidenceRefs,
    resultRefs,
    auditEvents,
    traceCompleteness: {
      hasAnswerPackage: Boolean(answerPackage),
      hasVerifier: Boolean(verifier),
      evidenceRefCount: evidenceRefs.length + evidence.length,
      resultRefCount: resultRefs.length,
      contractVersions: uniqueStrings(resultRefs.map((row) => row.contract_version)),
      snapshotIds: uniqueStrings(resultRefs.map((row) => row.snapshot_id)),
      queryRefs: uniqueStrings(resultRefs.map((row) => row.query_ref)),
    },
  };
}

function claimsFromPackage(answerPackage: unknown): unknown[] {
  const summary = summarySectionPayload(answerPackage);
  const claimGroups = summary?.claim_groups;
  if (Array.isArray(claimGroups) && claimGroups.length) return claimGroups;
  const claims = summary?.claims;
  return Array.isArray(claims) ? claims : [];
}

function evidenceFromPackage(answerPackage: unknown): unknown[] {
  const evidencePayload = evidenceSectionPayload(answerPackage);
  const evidence = evidencePayload?.evidence;
  return Array.isArray(evidence) ? evidence : [];
}

function verifierFromPackage(answerPackage: unknown) {
  if (!answerPackage || typeof answerPackage !== "object") return undefined;
  const admin = (answerPackage as Record<string, unknown>).admin_audit;
  if (!admin || typeof admin !== "object") return undefined;
  return (admin as Record<string, unknown>).verifier;
}

function evidenceSectionPayload(answerPackage: unknown): Record<string, unknown> | undefined {
  if (!answerPackage || typeof answerPackage !== "object") return undefined;
  const sections = (answerPackage as Record<string, unknown>).sections;
  if (!Array.isArray(sections)) return undefined;
  const evidence = sections.find((section) => {
    return Boolean(section && typeof section === "object" && (section as Record<string, unknown>).section_id === "evidence");
  });
  const payload = evidence && typeof evidence === "object" ? (evidence as Record<string, unknown>).payload : undefined;
  return payload && typeof payload === "object" ? payload as Record<string, unknown> : undefined;
}

function summarySectionPayload(answerPackage: unknown): Record<string, unknown> | undefined {
  if (!answerPackage || typeof answerPackage !== "object") return undefined;
  const sections = (answerPackage as Record<string, unknown>).sections;
  if (!Array.isArray(sections)) return undefined;
  const summary = sections.find((section) => {
    return Boolean(section && typeof section === "object" && (section as Record<string, unknown>).section_id === "summary");
  });
  const payload = summary && typeof summary === "object" ? (summary as Record<string, unknown>).payload : undefined;
  return payload && typeof payload === "object" ? payload as Record<string, unknown> : undefined;
}

function uniqueStrings(values: unknown[]) {
  return [...new Set(values.filter((value): value is string => typeof value === "string" && value.length > 0))];
}

function sameSet(left: string[], right: string[]) {
  return left.length === right.length && left.every((value) => right.includes(value));
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
  if (["clarification_policy_gate", "question_tool"].includes(nodeName)) {
    return {
      stage: "question",
      label: "判断是否需要用户确认",
      summary: "已检查当前问题是否需要用户补充口径、范围或业务目标后继续。",
      status,
    };
  }
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
  if (nodeName === "execute_capabilities") {
    return {
      stage: "capability_progress",
      label: "证据路径推进",
      summary: capabilityProgressSummary(payload),
      status,
    };
  }
  if (nodeName === "reduce_evidence") {
    return {
      stage: "evidence_summary",
      label: "证据摘要已生成",
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
  if (nodeName.includes("skip")) {
    return {
      stage: "repair_or_degrade",
      label: "跳过不可用路径",
      summary: "已跳过当前证据不足、权限不足或不适合本轮问题的路径。",
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
