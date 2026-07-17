import { Pool } from "pg";
import { createHash } from "crypto";

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

const RUN_STATUSES = [
  "queued",
  "running",
  "running_workflow",
  "waiting_for_clarification",
  "completed",
  "completed_without_workflow",
  "failed",
] as const;

type RunStatus = (typeof RUN_STATUSES)[number];

type RunRecord = {
  id: string;
  threadId: string;
  topicId?: string | null;
  status: RunStatus;
  createdAt: string;
  request?: Record<string, unknown>;
};

type RunDispatchRecord = {
  dispatchId: string;
  producerKind:
    | "thread_message"
    | "artifact_continue"
    | "clarification_resume"
    | "clarification_retry";
  scopeRef: string;
  requestIdentity: string;
  requestDigest: string;
  requestPayload: Record<string, unknown>;
  threadId: string;
  runId: string;
  messageId: string | null;
  state: "pending" | "leased" | "running" | "terminal";
  ownerId: string | null;
  leaseEpoch: number;
  leaseExpiresAt: string | null;
  heartbeatAt: string | null;
  terminalStatus: string | null;
  failureReason: string | null;
};

type RunDispatchClaim = {
  message: MessageRecord;
  run: RunRecord;
  dispatch: RunDispatchRecord;
  replayed: boolean;
};

type RunDispatchLease = {
  acquired: boolean;
  ownerId: string | null;
  leaseEpoch: number;
  state: RunDispatchRecord["state"];
  reason: "acquired" | "active_lease" | "already_running" | "terminal" | "run_not_queued";
  run: RunRecord;
};

type ClarificationResolutionRecord = {
  resolutionId: string;
  sourceRunId: string;
  threadId: string;
  topicId: string;
  ownerId: string;
  submission: {
    sourceRunId: string;
    answer: string;
    selectedOptionId: string | null;
    source: "user";
  };
  acceptedChoice: Record<string, unknown>;
  messageId: string;
  sourceRequestDigest: string;
  resolutionDigest: string;
  status: "accepted";
  createdAt: string;
  acceptedAt: string;
};

type ClarificationExecutionAttemptRecord = {
  attemptRunId: string;
  resolutionId: string;
  previousAttemptRunId: string | null;
  attemptNumber: number;
  requestIdentity: string;
  requestDigest: string;
  createdAt: string;
};

type ClarificationExecutionClaim = {
  sourceRunId: string;
  resolutionId: string;
  attemptRunId: string;
  previousAttemptRunId: string | null;
  attemptNumber: number;
  threadId: string;
  topicId: string;
  ownerId: string;
  requestIdentity: string;
  answer: string;
  selectedOptionId: string | null;
  source: "user";
  acceptedChoice: Record<string, unknown>;
  message: MessageRecord | null;
  run: RunRecord;
  replayed: boolean;
};

type MemoryAuditEvent = {
  eventType: string;
  actorId?: string;
  threadId?: string;
  topicId?: string;
  runId?: string;
  ref?: string;
  payload?: unknown;
};

type MemoryProposalRecord = {
  id: string;
  threadId: string;
  ownerId: string;
  text: string;
  status: "proposed" | "accepted" | "rejected";
  createdAt: string;
};

type ArtifactRecord = {
  id: string;
  threadId: string;
  topicId: string;
  snapshotId: string;
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
  clarificationResolutions: Map<string, ClarificationResolutionRecord>;
  clarificationExecutionAttempts: Map<string, ClarificationExecutionAttemptRecord>;
  runDispatches: Map<string, RunDispatchRecord>;
  auditEvents: MemoryAuditEvent[];
};

export class GatewayRuntimeError extends Error {
  readonly code: string;
  readonly httpStatus: number;

  constructor(code: string, httpStatus: number) {
    super(code);
    this.name = "GatewayRuntimeError";
    this.code = code;
    this.httpStatus = httpStatus;
  }
}

export function gatewayError(code: string): GatewayRuntimeError {
  return new GatewayRuntimeError(code, gatewayHttpStatus(code));
}

const globalStore = globalThis as typeof globalThis & {
  __wajeConversationMemoryStore?: MemoryStore;
  __wajeConversationPool?: Pool;
};

export function conversationStoreMode() {
  const databaseUrl = process.env.WAJE_RUNTIME_DATABASE_URL || process.env.DATABASE_URL;
  if (databaseUrl) return "postgres";
  if (
    process.env.NODE_ENV === "test"
    && process.env.WAJE_GATEWAY_UNIT_TEST_STORE === "memory"
  ) {
    return "memory";
  }
  throw new Error("WAJE_RUNTIME_DATABASE_URL or DATABASE_URL is required");
}

export async function listThreads(actorId: string): Promise<ThreadRecord[]> {
  actorId = normalizeActorId(actorId);
  if (conversationStoreMode() === "postgres") {
    const { rows } = await pool().query(`
      SELECT thread_id, owner_id, created_at
      FROM waje_runtime.investigation_threads
      WHERE owner_id = $1
      ORDER BY created_at DESC
    `, [actorId]);
    return rows.map((row) => ({
      id: row.thread_id,
      ownerId: row.owner_id,
      topicIds: [],
      messages: [],
      createdAt: row.created_at,
    }));
  }
  return [...memoryStore().threads.values()].filter((thread) => thread.ownerId === actorId);
}

export async function createThread(ownerId: string): Promise<ThreadRecord> {
  ownerId = normalizeActorId(ownerId);
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
    await audit("thread_created", {
      actorId: ownerId,
      threadId: thread.id,
      ref: thread.id,
      payload: { ownerId },
    });
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

export async function requireThread(threadId: string, actorId: string): Promise<ThreadRecord> {
  actorId = normalizeActorId(actorId);
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
    if (!row) throw gatewayError("thread_not_found");
    if (row.owner_id !== actorId) throw gatewayError("thread_owner_mismatch");
    return {
      id: row.thread_id,
      ownerId: row.owner_id,
      topicIds: [],
      messages: [],
      createdAt: row.created_at,
    } satisfies ThreadRecord;
  }
  const thread = memoryStore().threads.get(threadId);
  if (!thread) throw gatewayError("thread_not_found");
  if (thread.ownerId !== actorId) throw gatewayError("thread_owner_mismatch");
  return thread;
}

export async function createRun(threadId: string, actorId: string): Promise<RunRecord> {
  await requireThread(threadId, actorId);
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
    await audit("run_queued", { actorId, threadId, runId: run.id, ref: run.id });
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

export async function claimRunDispatchRequest(input: {
  producerKind: RunDispatchRecord["producerKind"];
  scopeRef: string;
  requestIdentity: string;
  threadId: string;
  text: string;
  actorId: string;
  requestPayload?: Record<string, unknown>;
}): Promise<RunDispatchClaim> {
  const normalized = normalizeRunDispatchInput(input);
  const requestDigest = runDispatchRequestDigest(normalized);
  if (conversationStoreMode() === "postgres") {
    const client = await pool().connect();
    try {
      await client.query("BEGIN");
      await client.query(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        [runDispatchIdentityLock(normalized)],
      );
      const existingResult = await client.query(
        `
        SELECT d.*, m.role, m.text, m.created_at AS message_created_at,
               r.status, r.request, r.created_at AS run_created_at,
               t.owner_id AS thread_owner_id
        FROM waje_runtime.run_dispatches d
        JOIN waje_runtime.conversation_messages m ON m.message_id = d.message_id
        JOIN waje_runtime.analysis_runs r ON r.run_id = d.run_id
        JOIN waje_runtime.investigation_threads t ON t.thread_id = d.thread_id
        WHERE d.producer_kind = $1
          AND d.scope_ref = $2
          AND d.request_identity = $3
        `,
        [normalized.producerKind, normalized.scopeRef, normalized.requestIdentity],
      );
      const existing = existingResult.rows[0];
      if (existing) {
        if (existing.thread_owner_id !== normalized.actorId) {
          throw gatewayError("thread_owner_mismatch");
        }
        if (
          existing.request_digest !== requestDigest
          || existing.thread_id !== normalized.threadId
        ) {
          throw gatewayError("run_dispatch_conflict");
        }
        await client.query("COMMIT");
        return runDispatchClaimFromRow(existing, true);
      }
      const threadResult = await client.query(
        `SELECT thread_id, owner_id FROM waje_runtime.investigation_threads
         WHERE thread_id = $1 FOR UPDATE`,
        [normalized.threadId],
      );
      if (!threadResult.rows[0]) throw gatewayError("thread_not_found");
      if (threadResult.rows[0].owner_id !== normalized.actorId) {
        throw gatewayError("thread_owner_mismatch");
      }
      const createdAt = new Date().toISOString();
      const messageId = `message-${crypto.randomUUID()}`;
      const runId = `run-${crypto.randomUUID()}`;
      const dispatchId = `dispatch-${crypto.randomUUID()}`;
      await client.query(
        `INSERT INTO waje_runtime.conversation_messages(message_id, thread_id, role, text)
         VALUES ($1, $2, 'user', $3)`,
        [messageId, normalized.threadId, normalized.text],
      );
      await client.query(
        `INSERT INTO waje_runtime.analysis_runs(run_id, thread_id, status)
         VALUES ($1, $2, 'queued')`,
        [runId, normalized.threadId],
      );
      await client.query(
        `
        INSERT INTO waje_runtime.run_dispatches(
          dispatch_id, producer_kind, scope_ref, request_identity,
          request_digest, request_payload, thread_id, run_id, message_id
        ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
        `,
        [
          dispatchId,
          normalized.producerKind,
          normalized.scopeRef,
          normalized.requestIdentity,
          requestDigest,
          JSON.stringify(normalized.requestPayload),
          normalized.threadId,
          runId,
          messageId,
        ],
      );
      await client.query(
        `
        INSERT INTO waje_runtime.audit_events(event_type, actor_id, thread_id, run_id, ref, payload)
        VALUES
          ('message_recorded', $1, $2, $3, $4, $5::jsonb),
          ('run_queued', $1, $2, $3, $3, $6::jsonb)
        `,
        [
          normalized.actorId,
          normalized.threadId,
          runId,
          messageId,
          JSON.stringify({ producerKind: normalized.producerKind }),
          JSON.stringify({ dispatchId, producerKind: normalized.producerKind }),
        ],
      );
      await client.query("COMMIT");
      return {
        message: { id: messageId, role: "user", text: normalized.text, createdAt },
        run: {
          id: runId,
          threadId: normalized.threadId,
          status: "queued",
          createdAt,
          request: {},
        },
        dispatch: {
          dispatchId,
          producerKind: normalized.producerKind,
          scopeRef: normalized.scopeRef,
          requestIdentity: normalized.requestIdentity,
          requestDigest,
          requestPayload: normalized.requestPayload,
          threadId: normalized.threadId,
          runId,
          messageId,
          state: "pending",
          ownerId: null,
          leaseEpoch: 0,
          leaseExpiresAt: null,
          heartbeatAt: null,
          terminalStatus: null,
          failureReason: null,
        },
        replayed: false,
      };
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  }

  const store = memoryStore();
  const existing = [...store.runDispatches.values()].find(
    (dispatch) => dispatch.producerKind === normalized.producerKind
      && dispatch.scopeRef === normalized.scopeRef
      && dispatch.requestIdentity === normalized.requestIdentity,
  );
  if (existing) {
    if (
      existing.requestDigest !== requestDigest
      || existing.threadId !== normalized.threadId
    ) {
      throw gatewayError("run_dispatch_conflict");
    }
    const run = store.runs.get(existing.runId);
    const thread = store.threads.get(existing.threadId);
    if (thread && thread.ownerId !== normalized.actorId) {
      throw gatewayError("thread_owner_mismatch");
    }
    const message = thread?.messages.find((item) => item.id === existing.messageId);
    if (!run || !message) throw gatewayError("run_dispatch_invariant_failed");
    return { message, run, dispatch: existing, replayed: true };
  }
  const thread = store.threads.get(normalized.threadId);
  if (!thread) throw gatewayError("thread_not_found");
  if (thread.ownerId !== normalized.actorId) throw gatewayError("thread_owner_mismatch");
  const createdAt = new Date().toISOString();
  const message: MessageRecord = {
    id: `message-${crypto.randomUUID()}`,
    role: "user",
    text: normalized.text,
    createdAt,
  };
  const run: RunRecord = {
    id: `run-${crypto.randomUUID()}`,
    threadId: normalized.threadId,
    status: "queued",
    createdAt,
    request: {},
  };
  const dispatch: RunDispatchRecord = {
    dispatchId: `dispatch-${crypto.randomUUID()}`,
    producerKind: normalized.producerKind,
    scopeRef: normalized.scopeRef,
    requestIdentity: normalized.requestIdentity,
    requestDigest,
    requestPayload: normalized.requestPayload,
    threadId: normalized.threadId,
    runId: run.id,
    messageId: message.id,
    state: "pending",
    ownerId: null,
    leaseEpoch: 0,
    leaseExpiresAt: null,
    heartbeatAt: null,
    terminalStatus: null,
    failureReason: null,
  };
  const stagedAudits: MemoryAuditEvent[] = [
    ...store.auditEvents,
    {
      eventType: "message_recorded",
      actorId: normalized.actorId,
      threadId: normalized.threadId,
      runId: run.id,
      ref: message.id,
      payload: { producerKind: normalized.producerKind },
    },
    {
      eventType: "run_queued",
      actorId: normalized.actorId,
      threadId: normalized.threadId,
      runId: run.id,
      ref: run.id,
      payload: { dispatchId: dispatch.dispatchId, producerKind: normalized.producerKind },
    },
  ];
  thread.messages.push(message);
  store.runs.set(run.id, run);
  store.runDispatches.set(run.id, dispatch);
  store.auditEvents = stagedAudits;
  return { message, run, dispatch, replayed: false };
}

export async function acquireRunDispatchLease(input: {
  runId: string;
  requestIdentity: string;
}): Promise<RunDispatchLease> {
  const ownerId = `gateway-dispatch-${crypto.randomUUID()}`;
  const leaseMs = runDispatchLeaseMs();
  if (conversationStoreMode() === "postgres") {
    const client = await pool().connect();
    try {
      await client.query("BEGIN");
      const dispatchResult = await client.query(
        `SELECT *, lease_expires_at > now() AS lease_active
         FROM waje_runtime.run_dispatches
         WHERE run_id = $1 FOR UPDATE`,
        [input.runId],
      );
      const row = dispatchResult.rows[0];
      if (!row) throw gatewayError("run_dispatch_not_found");
      if (row.request_identity !== input.requestIdentity) {
        throw gatewayError("run_dispatch_conflict");
      }
      const runResult = await client.query(
        `SELECT run_id, thread_id, status, request, created_at
         FROM waje_runtime.analysis_runs WHERE run_id = $1 FOR UPDATE`,
        [input.runId],
      );
      const run = runRecordFromRow(runResult.rows[0]);
      const state = runDispatchState(row.dispatch_state);
      if (state === "terminal") {
        await client.query("COMMIT");
        return { acquired: false, ownerId: null, leaseEpoch: Number(row.lease_epoch), state, reason: "terminal", run };
      }
      if (state === "running") {
        await client.query("COMMIT");
        return { acquired: false, ownerId: null, leaseEpoch: Number(row.lease_epoch), state, reason: "already_running", run };
      }
      if (run.status !== "queued") {
        await client.query("COMMIT");
        return { acquired: false, ownerId: null, leaseEpoch: Number(row.lease_epoch), state, reason: "run_not_queued", run };
      }
      if (state === "leased" && row.lease_active === true) {
        await client.query("COMMIT");
        return { acquired: false, ownerId: null, leaseEpoch: Number(row.lease_epoch), state, reason: "active_lease", run };
      }
      const updated = await client.query(
        `
        UPDATE waje_runtime.run_dispatches
        SET dispatch_state = 'leased', owner_id = $2,
            lease_epoch = lease_epoch + 1,
            lease_expires_at = now() + ($3 * interval '1 millisecond'),
            heartbeat_at = now(), updated_at = now()
        WHERE run_id = $1
        RETURNING lease_epoch
        `,
        [input.runId, ownerId, leaseMs],
      );
      await client.query("COMMIT");
      return { acquired: true, ownerId, leaseEpoch: Number(updated.rows[0].lease_epoch), state: "leased", reason: "acquired", run };
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  }
  const store = memoryStore();
  const dispatch = store.runDispatches.get(input.runId);
  const run = store.runs.get(input.runId);
  if (!dispatch || !run) throw gatewayError("run_dispatch_not_found");
  if (dispatch.requestIdentity !== input.requestIdentity) {
    throw gatewayError("run_dispatch_conflict");
  }
  if (dispatch.state === "terminal") {
    return { acquired: false, ownerId: null, leaseEpoch: dispatch.leaseEpoch, state: dispatch.state, reason: "terminal", run };
  }
  if (dispatch.state === "running") {
    return { acquired: false, ownerId: null, leaseEpoch: dispatch.leaseEpoch, state: dispatch.state, reason: "already_running", run };
  }
  if (run.status !== "queued") {
    return { acquired: false, ownerId: null, leaseEpoch: dispatch.leaseEpoch, state: dispatch.state, reason: "run_not_queued", run };
  }
  const expiry = dispatch.leaseExpiresAt ? Date.parse(dispatch.leaseExpiresAt) : 0;
  if (dispatch.state === "leased" && Number.isFinite(expiry) && expiry > Date.now()) {
    return { acquired: false, ownerId: null, leaseEpoch: dispatch.leaseEpoch, state: dispatch.state, reason: "active_lease", run };
  }
  dispatch.state = "leased";
  dispatch.ownerId = ownerId;
  dispatch.leaseEpoch += 1;
  dispatch.leaseExpiresAt = new Date(Date.now() + leaseMs).toISOString();
  dispatch.heartbeatAt = new Date().toISOString();
  return { acquired: true, ownerId, leaseEpoch: dispatch.leaseEpoch, state: "leased", reason: "acquired", run };
}

export async function failOwnedRunDispatch(input: {
  runId: string;
  ownerId: string;
  leaseEpoch: number;
  failureReason: string;
}): Promise<RunRecord> {
  if (conversationStoreMode() === "postgres") {
    const client = await pool().connect();
    try {
      await client.query("BEGIN");
      const dispatchResult = await client.query(
        `SELECT * FROM waje_runtime.run_dispatches
         WHERE run_id = $1 FOR UPDATE`,
        [input.runId],
      );
      const dispatch = dispatchResult.rows[0];
      if (!dispatch) throw gatewayError("run_dispatch_not_found");
      const runResult = await client.query(
        `SELECT run_id, thread_id, status, request, created_at
         FROM waje_runtime.analysis_runs WHERE run_id = $1 FOR UPDATE`,
        [input.runId],
      );
      const current = runRecordFromRow(runResult.rows[0]);
      if (
        !["leased", "running"].includes(String(dispatch.dispatch_state))
        || dispatch.owner_id !== input.ownerId
        || Number(dispatch.lease_epoch) !== input.leaseEpoch
        || !["queued", "running", "running_workflow"].includes(current.status)
      ) {
        await client.query("COMMIT");
        return current;
      }
      const failedResult = await client.query(
        `UPDATE waje_runtime.analysis_runs
         SET status = 'failed',
             request = COALESCE(request, '{}'::jsonb)
               || jsonb_build_object('failure_reason', $2),
             updated_at = now()
         WHERE run_id = $1 AND status IN ('queued', 'running', 'running_workflow')
         RETURNING run_id, thread_id, status, request, created_at`,
        [input.runId, input.failureReason],
      );
      if (!failedResult.rows[0]) throw gatewayError("run_dispatch_lease_lost");
      await client.query(
        `UPDATE waje_runtime.run_dispatches
         SET dispatch_state = 'terminal', terminal_status = 'failed',
             failure_reason = $4, lease_expires_at = NULL, updated_at = now()
         WHERE run_id = $1 AND owner_id = $2 AND lease_epoch = $3`,
        [input.runId, input.ownerId, input.leaseEpoch, input.failureReason],
      );
      await client.query(
        `INSERT INTO waje_runtime.audit_events(event_type, actor_id, thread_id, run_id, ref, payload)
         VALUES
           ('run_status_changed', 'system', $1, $2, $2, $3::jsonb),
           ('run_dispatch_failed', 'system', $1, $2, $2, $4::jsonb)`,
        [
          failedResult.rows[0].thread_id,
          input.runId,
          JSON.stringify({ status: "failed" }),
          JSON.stringify({ failureReason: input.failureReason, leaseEpoch: input.leaseEpoch }),
        ],
      );
      await client.query("COMMIT");
      return runRecordFromRow(failedResult.rows[0]);
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  }
  const store = memoryStore();
  const dispatch = store.runDispatches.get(input.runId);
  const current = store.runs.get(input.runId);
  if (!dispatch || !current) throw gatewayError("run_dispatch_not_found");
  if (
    !["leased", "running"].includes(dispatch.state)
    || dispatch.ownerId !== input.ownerId
    || dispatch.leaseEpoch !== input.leaseEpoch
    || !["queued", "running", "running_workflow"].includes(current.status)
  ) {
    return current;
  }
  const failed: RunRecord = {
    ...current,
    status: "failed",
    request: { ...(current.request ?? {}), failure_reason: input.failureReason },
  };
  dispatch.state = "terminal";
  dispatch.terminalStatus = "failed";
  dispatch.failureReason = input.failureReason;
  dispatch.leaseExpiresAt = null;
  store.runs.set(input.runId, failed);
  store.auditEvents = [
    ...store.auditEvents,
    { eventType: "run_status_changed", threadId: current.threadId, runId: current.id, ref: current.id, payload: { status: "failed" } },
    { eventType: "run_dispatch_failed", threadId: current.threadId, runId: current.id, ref: current.id, payload: { failureReason: input.failureReason, leaseEpoch: input.leaseEpoch } },
  ];
  return failed;
}

export async function observeOwnedRunDispatchExit(input: {
  runId: string;
  ownerId: string;
  leaseEpoch: number;
  failureReason?: string;
}): Promise<RunRecord> {
  return failOwnedRunDispatch({
    ...input,
    failureReason: input.failureReason ?? "agent_core_worker_exited",
  });
}

export async function completeOwnedRunDispatch(input: {
  runId: string;
  ownerId: string;
  leaseEpoch: number;
  runStatus: "waiting_for_clarification" | "completed" | "completed_without_workflow" | "failed";
}): Promise<RunRecord> {
  if (conversationStoreMode() === "postgres") {
    const client = await pool().connect();
    try {
      await client.query("BEGIN");
      const dispatchResult = await client.query(
        `SELECT * FROM waje_runtime.run_dispatches
         WHERE run_id = $1 FOR UPDATE`,
        [input.runId],
      );
      const dispatch = dispatchResult.rows[0];
      if (!dispatch) throw gatewayError("run_dispatch_not_found");
      const runResult = await client.query(
        `SELECT run_id, thread_id, status, request, created_at
         FROM waje_runtime.analysis_runs WHERE run_id = $1 FOR UPDATE`,
        [input.runId],
      );
      let current = runRecordFromRow(runResult.rows[0]);
      const ownsDispatch = dispatch.owner_id === input.ownerId
        && Number(dispatch.lease_epoch) === input.leaseEpoch;
      if (!ownsDispatch) {
        await client.query("COMMIT");
        return current;
      }
      if (dispatch.dispatch_state === "terminal") {
        await client.query("COMMIT");
        return current;
      }
      if (!["pending", "leased", "running"].includes(String(dispatch.dispatch_state))) {
        throw gatewayError("run_dispatch_state_invalid");
      }
      const runCanFinish = ["queued", "running", "running_workflow"].includes(current.status);
      if (runCanFinish) {
        const updated = await client.query(
          `UPDATE waje_runtime.analysis_runs
           SET status = $2, updated_at = now()
           WHERE run_id = $1 AND status IN ('queued', 'running', 'running_workflow')
           RETURNING run_id, thread_id, status, request, created_at`,
          [input.runId, input.runStatus],
        );
        if (!updated.rows[0]) throw gatewayError("run_dispatch_lease_lost");
        current = runRecordFromRow(updated.rows[0]);
      }
      await client.query(
        `UPDATE waje_runtime.run_dispatches
         SET dispatch_state = 'terminal', terminal_status = $4,
             lease_expires_at = NULL, heartbeat_at = now(), updated_at = now()
         WHERE run_id = $1 AND owner_id = $2 AND lease_epoch = $3
           AND dispatch_state IN ('leased', 'running')`,
        [input.runId, input.ownerId, input.leaseEpoch, current.status],
      );
      await client.query(
        `INSERT INTO waje_runtime.audit_events(event_type, actor_id, thread_id, run_id, ref, payload)
         VALUES ('run_dispatch_completed', 'system', $1, $2, $2, $3::jsonb)`,
        [
          current.threadId,
          input.runId,
          JSON.stringify({ status: current.status, leaseEpoch: input.leaseEpoch }),
        ],
      );
      await client.query("COMMIT");
      return current;
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  }
  const store = memoryStore();
  const dispatch = store.runDispatches.get(input.runId);
  const current = store.runs.get(input.runId);
  if (!dispatch || !current) throw gatewayError("run_dispatch_not_found");
  if (
    dispatch.ownerId !== input.ownerId
    || dispatch.leaseEpoch !== input.leaseEpoch
    || dispatch.state === "terminal"
  ) {
    return current;
  }
  if (!["leased", "running"].includes(dispatch.state)) {
    throw gatewayError("run_dispatch_state_invalid");
  }
  const completed = ["queued", "running", "running_workflow"].includes(current.status)
    ? { ...current, status: input.runStatus }
    : current;
  dispatch.state = "terminal";
  dispatch.terminalStatus = completed.status;
  dispatch.leaseExpiresAt = null;
  dispatch.heartbeatAt = new Date().toISOString();
  store.runs.set(input.runId, completed);
  store.auditEvents = [
    ...store.auditEvents,
    {
      eventType: "run_dispatch_completed",
      threadId: completed.threadId,
      runId: input.runId,
      ref: input.runId,
      payload: { status: completed.status, leaseEpoch: input.leaseEpoch },
    },
  ];
  return completed;
}

export function runDispatchRequestIdentity(
  request: Request,
  body: Record<string, unknown>,
): string {
  const headerIdentity = request.headers.get("idempotency-key")?.trim() ?? "";
  const bodyValue = body.requestIdentity;
  if (bodyValue !== undefined && !isNonEmptyGatewayString(bodyValue)) {
    throw gatewayError("run_dispatch_request_identity_invalid");
  }
  const bodyIdentity = typeof bodyValue === "string" ? bodyValue.trim() : "";
  if (headerIdentity && bodyIdentity && headerIdentity !== bodyIdentity) {
    throw gatewayError("run_dispatch_request_identity_conflict");
  }
  const identity = headerIdentity || bodyIdentity;
  if (!identity || identity.length > 256) {
    throw gatewayError("run_dispatch_request_identity_required");
  }
  return identity;
}

export async function claimClarificationResolutionAttempt(input: {
  sourceRunId: string;
  requestIdentity: string;
  answer: string;
  selectedOptionId?: string | null;
  source?: string;
  actorId: string;
}): Promise<ClarificationExecutionClaim> {
  const normalized = normalizeClarificationResolutionInput(input);
  const sourceRunId = normalized.sourceRunId;
  const requestIdentity = normalized.requestIdentity;
  const answer = normalized.answer;
  const selectedOptionId = normalized.selectedOptionId;
  const source = normalized.source;
  const actorId = normalizeActorId(input.actorId);
  if (conversationStoreMode() === "postgres") {
    const client = await pool().connect();
    try {
      await client.query("BEGIN");
      await client.query(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        [`clarification-resolution:${sourceRunId}`],
      );
      const sourceResult = await client.query(
        `
        SELECT r.run_id, r.thread_id, r.topic_id, r.status, r.request,
               t.owner_id
        FROM waje_runtime.analysis_runs r
        JOIN waje_runtime.investigation_threads t ON t.thread_id = r.thread_id
        WHERE r.run_id = $1
        FOR UPDATE
        `,
        [sourceRunId],
      );
      const sourceRun = sourceResult.rows[0];
      if (!sourceRun) throw gatewayError("run_not_found");
      if (sourceRun.owner_id !== actorId) throw gatewayError("run_owner_mismatch");
      if (!isNonEmptyGatewayString(sourceRun.topic_id)) {
        throw gatewayError("clarification_source_topic_invalid");
      }
      const acceptedChoice = resolveAcceptedClarificationChoice(
        sourceRun.request,
        answer,
        selectedOptionId,
      );
      const submission = {
        sourceRunId,
        answer,
        selectedOptionId,
        source,
      };
      const sourceRequestDigest = gatewayValueDigest(sourceRun.request ?? {});
      const existingResult = await client.query(
        `
        SELECT c.*, a.attempt_run_id, a.previous_attempt_run_id,
               a.attempt_number, a.request_identity, a.request_digest,
               m.role, m.text, m.created_at AS message_created_at,
               r.status AS attempt_status, r.request AS attempt_request,
               r.created_at AS run_created_at
        FROM waje_runtime.clarification_resolutions c
        JOIN waje_runtime.clarification_execution_attempts a
          ON a.resolution_id = c.resolution_id AND a.attempt_number = 1
        JOIN waje_runtime.conversation_messages m ON m.message_id = c.message_id
        JOIN waje_runtime.analysis_runs r ON r.run_id = a.attempt_run_id
        WHERE c.source_run_id = $1
        `,
        [sourceRunId],
      );
      const existing = existingResult.rows[0];
      if (existing) {
        const expectedResolutionDigest = clarificationResolutionDigest({
          resolutionId: String(existing.resolution_id),
          sourceRunId,
          threadId: String(sourceRun.thread_id),
          topicId: String(sourceRun.topic_id),
          ownerId: actorId,
          submission,
          acceptedChoice,
          messageId: String(existing.message_id),
          sourceRequestDigest,
        });
        const normalizedDispatch = clarificationDispatchInput({
          producerKind: "clarification_resume",
          scopeRef: String(existing.resolution_id),
          requestIdentity,
          threadId: String(sourceRun.thread_id),
          actorId,
          answer,
          resolutionId: String(existing.resolution_id),
          sourceRunId,
          attemptRunId: String(existing.attempt_run_id),
          previousAttemptRunId: null,
          selectedOptionId,
          retryAttempt: false,
        });
        if (
          existing.request_identity !== requestIdentity
          || existing.request_digest !== runDispatchRequestDigest(normalizedDispatch)
          || existing.source_request_digest !== sourceRequestDigest
          || existing.resolution_digest !== expectedResolutionDigest
          || existing.thread_id !== sourceRun.thread_id
          || existing.topic_id !== sourceRun.topic_id
          || existing.owner_id !== actorId
          || existing.status !== "accepted"
          || !existing.accepted_at
        ) {
          throw gatewayError("clarification_resolution_conflict");
        }
        await client.query("COMMIT");
        return clarificationExecutionClaimFromRow(existing, true);
      }
      if (sourceRun.status !== "waiting_for_clarification") {
        throw gatewayError("clarification_source_not_waiting");
      }

      const resolutionId = `clarification-resolution-${crypto.randomUUID()}`;
      const attemptRunId = `run-${crypto.randomUUID()}`;
      const messageId = `message-${crypto.randomUUID()}`;
      const dispatchId = `dispatch-${crypto.randomUUID()}`;
      const createdAt = new Date().toISOString();
      const resolutionDigest = clarificationResolutionDigest({
        resolutionId,
        sourceRunId,
        threadId: String(sourceRun.thread_id),
        topicId: String(sourceRun.topic_id),
        ownerId: actorId,
        submission,
        acceptedChoice,
        messageId,
        sourceRequestDigest,
      });
      const normalizedDispatch = clarificationDispatchInput({
        producerKind: "clarification_resume",
        scopeRef: resolutionId,
        requestIdentity,
        threadId: String(sourceRun.thread_id),
        actorId,
        answer,
        resolutionId,
        sourceRunId,
        attemptRunId,
        previousAttemptRunId: null,
        selectedOptionId,
        retryAttempt: false,
      });
      const requestDigest = runDispatchRequestDigest(normalizedDispatch);
      await client.query(
        `
        INSERT INTO waje_runtime.conversation_messages(message_id, thread_id, role, text)
        VALUES ($1, $2, 'user', $3)
        `,
        [messageId, sourceRun.thread_id, answer],
      );
      await client.query(
        `
        INSERT INTO waje_runtime.analysis_runs(
          run_id, thread_id, topic_id, status, request
        ) VALUES ($1, $2, $3, 'queued', $4::jsonb)
        `,
        [
          attemptRunId,
          sourceRun.thread_id,
          sourceRun.topic_id,
          JSON.stringify(clarificationAttemptRunRequest({
            resolutionId,
            sourceRunId,
            attemptNumber: 1,
            previousAttemptRunId: null,
          })),
        ],
      );
      await client.query(
        `
        INSERT INTO waje_runtime.clarification_resolutions(
          resolution_id, source_run_id, thread_id, topic_id, owner_id,
          submission, accepted_choice, message_id, source_request_digest,
          resolution_digest
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9, $10)
        `,
        [
          resolutionId,
          sourceRunId,
          sourceRun.thread_id,
          sourceRun.topic_id,
          actorId,
          JSON.stringify(submission),
          JSON.stringify(acceptedChoice),
          messageId,
          sourceRequestDigest,
          resolutionDigest,
        ],
      );
      await client.query(
        `INSERT INTO waje_runtime.clarification_execution_attempts(
           attempt_run_id, resolution_id, previous_attempt_run_id,
           attempt_number, request_identity, request_digest
         ) VALUES ($1, $2, NULL, 1, $3, $4)`,
        [attemptRunId, resolutionId, requestIdentity, requestDigest],
      );
      await client.query(
        `INSERT INTO waje_runtime.run_dispatches(
           dispatch_id, producer_kind, scope_ref, request_identity,
           request_digest, request_payload, thread_id, run_id, message_id
         ) VALUES ($1, 'clarification_resume', $2, $3, $4, $5::jsonb, $6, $7, $8)`,
        [
          dispatchId,
          resolutionId,
          requestIdentity,
          requestDigest,
          JSON.stringify(normalizedDispatch.requestPayload),
          sourceRun.thread_id,
          attemptRunId,
          messageId,
        ],
      );
      await client.query(
        `
        INSERT INTO waje_runtime.audit_events(
          event_type, actor_id, thread_id, run_id, ref, payload
        ) VALUES
          ('clarification_answer_recorded', $1, $2, $3, $3, $5::jsonb),
          ('run_queued', $1, $2, $4, $4, $6::jsonb)
        `,
        [
          actorId,
          sourceRun.thread_id,
          sourceRunId,
          attemptRunId,
          JSON.stringify({
            resolutionId,
            submission,
            acceptedChoice,
          }),
          JSON.stringify({
            dispatchId,
            producerKind: "clarification_resume",
            resolutionId,
            attemptNumber: 1,
          }),
        ],
      );
      await client.query("COMMIT");
      return {
        sourceRunId,
        resolutionId,
        attemptRunId,
        previousAttemptRunId: null,
        attemptNumber: 1,
        threadId: sourceRun.thread_id,
        topicId: sourceRun.topic_id,
        ownerId: actorId,
        requestIdentity,
        answer,
        selectedOptionId,
        source,
        acceptedChoice,
        message: {
          id: messageId,
          role: "user",
          text: answer,
          createdAt,
        },
        run: {
          id: attemptRunId,
          threadId: sourceRun.thread_id,
          topicId: sourceRun.topic_id,
          status: "queued",
          createdAt,
          request: clarificationAttemptRunRequest({
            resolutionId,
            sourceRunId,
            attemptNumber: 1,
            previousAttemptRunId: null,
          }),
        },
        replayed: false,
      };
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  }

  const store = memoryStore();
  const sourceRun = store.runs.get(sourceRunId);
  if (!sourceRun) throw gatewayError("run_not_found");
  const sourceThread = store.threads.get(sourceRun.threadId);
  if (!sourceThread) throw gatewayError("thread_not_found");
  if (sourceThread.ownerId !== actorId) throw gatewayError("run_owner_mismatch");
  if (!isNonEmptyGatewayString(sourceRun.topicId)) {
    throw gatewayError("clarification_source_topic_invalid");
  }
  const acceptedChoice = resolveAcceptedClarificationChoice(
    sourceRun.request,
    answer,
    selectedOptionId,
  );
  const submission = {
    sourceRunId,
    answer,
    selectedOptionId,
    source,
  };
  const sourceRequestDigest = gatewayValueDigest(sourceRun.request ?? {});
  const existing = [...store.clarificationResolutions.values()].find(
    (resolution) => resolution.sourceRunId === sourceRunId,
  );
  if (existing) {
    const attempt = [...store.clarificationExecutionAttempts.values()].find(
      (candidate) => candidate.resolutionId === existing.resolutionId
        && candidate.attemptNumber === 1,
    );
    if (!attempt) throw gatewayError("clarification_resolution_conflict");
    const normalizedDispatch = clarificationDispatchInput({
      producerKind: "clarification_resume",
      scopeRef: existing.resolutionId,
      requestIdentity,
      threadId: sourceRun.threadId,
      actorId,
      answer,
      resolutionId: existing.resolutionId,
      sourceRunId,
      attemptRunId: attempt.attemptRunId,
      previousAttemptRunId: null,
      selectedOptionId,
      retryAttempt: false,
    });
    const expectedResolutionDigest = clarificationResolutionDigest({
      resolutionId: existing.resolutionId,
      sourceRunId,
      threadId: sourceRun.threadId,
      topicId: sourceRun.topicId,
      ownerId: actorId,
      submission,
      acceptedChoice,
      messageId: existing.messageId,
      sourceRequestDigest,
    });
    if (
      attempt.requestIdentity !== requestIdentity
      || attempt.requestDigest !== runDispatchRequestDigest(normalizedDispatch)
      || existing.threadId !== sourceRun.threadId
      || existing.topicId !== sourceRun.topicId
      || existing.ownerId !== actorId
      || existing.status !== "accepted"
      || !isNonEmptyGatewayString(existing.acceptedAt)
      || existing.sourceRequestDigest !== sourceRequestDigest
      || existing.resolutionDigest !== expectedResolutionDigest
    ) {
      throw gatewayError("clarification_resolution_conflict");
    }
    return clarificationExecutionClaimFromMemory(
      store,
      existing,
      attempt,
      true,
    );
  }
  if (sourceRun.status !== "waiting_for_clarification") {
    throw gatewayError("clarification_source_not_waiting");
  }
  const thread = sourceThread;
  const createdAt = new Date().toISOString();
  const resolutionId = `clarification-resolution-${crypto.randomUUID()}`;
  const message: MessageRecord = {
    id: `message-${crypto.randomUUID()}`,
    role: "user",
    text: answer,
    createdAt,
  };
  const run: RunRecord = {
    id: `run-${crypto.randomUUID()}`,
    threadId: sourceRun.threadId,
    topicId: sourceRun.topicId,
    status: "queued",
    createdAt,
    request: clarificationAttemptRunRequest({
      resolutionId,
      sourceRunId,
      attemptNumber: 1,
      previousAttemptRunId: null,
    }),
  };
  const resolution: ClarificationResolutionRecord = {
    resolutionId,
    sourceRunId,
    threadId: sourceRun.threadId,
    topicId: sourceRun.topicId,
    ownerId: actorId,
    submission,
    acceptedChoice,
    messageId: message.id,
    sourceRequestDigest,
    resolutionDigest: clarificationResolutionDigest({
      resolutionId,
      sourceRunId,
      threadId: sourceRun.threadId,
      topicId: sourceRun.topicId,
      ownerId: actorId,
      submission,
      acceptedChoice,
      messageId: message.id,
      sourceRequestDigest,
    }),
    status: "accepted",
    createdAt,
    acceptedAt: createdAt,
  };
  const normalizedDispatch = clarificationDispatchInput({
    producerKind: "clarification_resume",
    scopeRef: resolutionId,
    requestIdentity,
    threadId: sourceRun.threadId,
    actorId,
    answer,
    resolutionId,
    sourceRunId,
    attemptRunId: run.id,
    previousAttemptRunId: null,
    selectedOptionId,
    retryAttempt: false,
  });
  const requestDigest = runDispatchRequestDigest(normalizedDispatch);
  const attempt: ClarificationExecutionAttemptRecord = {
    attemptRunId: run.id,
    resolutionId,
    previousAttemptRunId: null,
    attemptNumber: 1,
    requestIdentity,
    requestDigest,
    createdAt,
  };
  const dispatch: RunDispatchRecord = {
    dispatchId: `dispatch-${crypto.randomUUID()}`,
    producerKind: "clarification_resume",
    scopeRef: resolutionId,
    requestIdentity,
    requestDigest,
    requestPayload: normalizedDispatch.requestPayload,
    threadId: sourceRun.threadId,
    runId: run.id,
    messageId: message.id,
    state: "pending",
    ownerId: null,
    leaseEpoch: 0,
    leaseExpiresAt: null,
    heartbeatAt: null,
    terminalStatus: null,
    failureReason: null,
  };
  thread.messages.push(message);
  store.runs.set(run.id, run);
  store.clarificationResolutions.set(resolutionId, resolution);
  store.clarificationExecutionAttempts.set(run.id, attempt);
  store.runDispatches.set(run.id, dispatch);
  store.auditEvents.push(
    {
      eventType: "clarification_answer_recorded",
      actorId,
      threadId: sourceRun.threadId,
      runId: sourceRunId,
      ref: sourceRunId,
      payload: {
        resolutionId,
        submission,
        acceptedChoice,
      },
    },
    {
      eventType: "run_queued",
      actorId,
      threadId: sourceRun.threadId,
      runId: run.id,
      ref: run.id,
      payload: {
        dispatchId: dispatch.dispatchId,
        producerKind: dispatch.producerKind,
      },
    },
  );
  return {
    sourceRunId,
    resolutionId,
    attemptRunId: run.id,
    previousAttemptRunId: null,
    attemptNumber: 1,
    threadId: sourceRun.threadId,
    topicId: sourceRun.topicId,
    ownerId: actorId,
    requestIdentity,
    answer,
    selectedOptionId,
    source,
    acceptedChoice,
    message,
    run,
    replayed: false,
  };
}

export async function claimClarificationRetryAttempt(input: {
  failedRunId: string;
  requestIdentity: string;
  actorId: string;
}): Promise<ClarificationExecutionClaim> {
  const failedRunId = normalizedGatewayIdentifier(
    input.failedRunId,
    "clarification_retry_run_invalid",
  );
  const requestIdentity = normalizedGatewayIdentifier(
    input.requestIdentity,
    "run_dispatch_request_identity_invalid",
    256,
  );
  const actorId = normalizeActorId(input.actorId);
  if (conversationStoreMode() === "postgres") {
    const client = await pool().connect();
    try {
      await client.query("BEGIN");
      await client.query(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        [`clarification-retry:${failedRunId}`],
      );
      const priorResult = await client.query(
        `
        SELECT c.*, a.attempt_run_id AS previous_attempt_run_id,
               a.attempt_number AS previous_attempt_number,
               failed.status AS previous_attempt_status,
               failed.thread_id AS previous_thread_id,
               failed.topic_id AS previous_topic_id,
               source.thread_id AS source_thread_id,
               source.topic_id AS source_topic_id,
               source.request AS source_request,
               t.owner_id AS current_owner_id,
               m.role, m.text, m.created_at AS message_created_at
        FROM waje_runtime.clarification_execution_attempts a
        JOIN waje_runtime.clarification_resolutions c
          ON c.resolution_id = a.resolution_id
        JOIN waje_runtime.analysis_runs failed
          ON failed.run_id = a.attempt_run_id
        JOIN waje_runtime.analysis_runs source
          ON source.run_id = c.source_run_id
        JOIN waje_runtime.investigation_threads t
          ON t.thread_id = c.thread_id
        JOIN waje_runtime.conversation_messages m
          ON m.message_id = c.message_id
        WHERE a.attempt_run_id = $1
        FOR UPDATE OF a, c, failed, source
        `,
        [failedRunId],
      );
      const prior = priorResult.rows[0];
      if (!prior) throw gatewayError("clarification_attempt_not_found");
      assertRetryResolutionAuthority(prior, actorId);

      const sourceRunId = String(prior.source_run_id);
      const resolutionId = String(prior.resolution_id);
      const submission = canonicalGatewayRecord(prior.submission ?? {});
      const acceptedChoice = canonicalGatewayRecord(prior.accepted_choice ?? {});
      const answer = String(submission.answer ?? "");
      const selectedOptionId = typeof submission.selectedOptionId === "string"
        ? submission.selectedOptionId
        : null;
      const existingResult = await client.query(
        `
        SELECT c.*, successor.attempt_run_id,
               successor.previous_attempt_run_id, successor.attempt_number,
               successor.request_identity, successor.request_digest,
               r.status AS attempt_status, r.request AS attempt_request,
               r.created_at AS run_created_at,
               m.role, m.text, m.created_at AS message_created_at
        FROM waje_runtime.clarification_execution_attempts successor
        JOIN waje_runtime.clarification_resolutions c
          ON c.resolution_id = successor.resolution_id
        JOIN waje_runtime.analysis_runs r
          ON r.run_id = successor.attempt_run_id
        JOIN waje_runtime.conversation_messages m
          ON m.message_id = c.message_id
        WHERE successor.resolution_id = $1
          AND (
            successor.previous_attempt_run_id = $2
            OR successor.request_identity = $3
          )
        `,
        [resolutionId, failedRunId, requestIdentity],
      );
      const existing = existingResult.rows[0];
      const attemptNumber = existing
        ? Number(existing.attempt_number)
        : Number(prior.previous_attempt_number) + 1;
      const attemptRunId = existing
        ? String(existing.attempt_run_id)
        : `run-${crypto.randomUUID()}`;
      const normalizedDispatch = clarificationDispatchInput({
        producerKind: "clarification_retry",
        scopeRef: resolutionId,
        requestIdentity,
        threadId: String(prior.thread_id),
        actorId,
        answer,
        resolutionId,
        sourceRunId,
        attemptRunId,
        previousAttemptRunId: failedRunId,
        selectedOptionId,
        retryAttempt: true,
      });
      const requestDigest = runDispatchRequestDigest(normalizedDispatch);
      if (existing) {
        if (
          existing.previous_attempt_run_id !== failedRunId
          || existing.request_identity !== requestIdentity
          || existing.request_digest !== requestDigest
        ) {
          throw gatewayError("clarification_retry_conflict");
        }
        await client.query("COMMIT");
        return clarificationExecutionClaimFromRow(existing, true, null);
      }

      const dispatchId = `dispatch-${crypto.randomUUID()}`;
      const createdAt = new Date().toISOString();
      const runRequest = clarificationAttemptRunRequest({
        resolutionId,
        sourceRunId,
        attemptNumber,
        previousAttemptRunId: failedRunId,
        retryAttempt: true,
      });
      await client.query(
        `INSERT INTO waje_runtime.analysis_runs(
           run_id, thread_id, topic_id, status, request
         ) VALUES ($1, $2, $3, 'queued', $4::jsonb)`,
        [
          attemptRunId,
          prior.thread_id,
          prior.topic_id,
          JSON.stringify(runRequest),
        ],
      );
      await client.query(
        `INSERT INTO waje_runtime.clarification_execution_attempts(
           attempt_run_id, resolution_id, previous_attempt_run_id,
           attempt_number, request_identity, request_digest
         ) VALUES ($1, $2, $3, $4, $5, $6)`,
        [
          attemptRunId,
          resolutionId,
          failedRunId,
          attemptNumber,
          requestIdentity,
          requestDigest,
        ],
      );
      await client.query(
        `INSERT INTO waje_runtime.run_dispatches(
           dispatch_id, producer_kind, scope_ref, request_identity,
           request_digest, request_payload, thread_id, run_id, message_id
         ) VALUES ($1, 'clarification_retry', $2, $3, $4, $5::jsonb, $6, $7, NULL)`,
        [
          dispatchId,
          resolutionId,
          requestIdentity,
          requestDigest,
          JSON.stringify(normalizedDispatch.requestPayload),
          prior.thread_id,
          attemptRunId,
        ],
      );
      await client.query(
        `INSERT INTO waje_runtime.audit_events(
           event_type, actor_id, thread_id, topic_id, run_id, ref, payload
         ) VALUES
           ('clarification_retry_queued', $1, $2, $3, $4, $5, $6::jsonb),
           ('run_queued', $1, $2, $3, $4, $4, $7::jsonb)`,
        [
          actorId,
          prior.thread_id,
          prior.topic_id,
          attemptRunId,
          failedRunId,
          JSON.stringify({
            resolutionId,
            previousAttemptRunId: failedRunId,
            attemptNumber,
          }),
          JSON.stringify({
            dispatchId,
            producerKind: "clarification_retry",
            resolutionId,
            previousAttemptRunId: failedRunId,
            attemptNumber,
          }),
        ],
      );
      await client.query("COMMIT");
      return {
        sourceRunId,
        resolutionId,
        attemptRunId,
        previousAttemptRunId: failedRunId,
        attemptNumber,
        threadId: String(prior.thread_id),
        topicId: String(prior.topic_id),
        ownerId: actorId,
        requestIdentity,
        answer,
        selectedOptionId,
        source: "user",
        acceptedChoice,
        message: null,
        run: {
          id: attemptRunId,
          threadId: String(prior.thread_id),
          topicId: String(prior.topic_id),
          status: "queued",
          createdAt,
          request: runRequest,
        },
        replayed: false,
      };
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  }

  const store = memoryStore();
  const priorAttempt = store.clarificationExecutionAttempts.get(failedRunId);
  if (!priorAttempt) throw gatewayError("clarification_attempt_not_found");
  const resolution = store.clarificationResolutions.get(priorAttempt.resolutionId);
  const failedRun = store.runs.get(failedRunId);
  if (!resolution || !failedRun) {
    throw gatewayError("clarification_attempt_not_found");
  }
  assertMemoryRetryResolutionAuthority(store, resolution, failedRun, actorId);
  const successor = [...store.clarificationExecutionAttempts.values()].find(
    (attempt) => attempt.resolutionId === resolution.resolutionId
      && attempt.previousAttemptRunId === failedRunId,
  );
  const identityAttempt = [...store.clarificationExecutionAttempts.values()].find(
    (attempt) => attempt.resolutionId === resolution.resolutionId
      && attempt.requestIdentity === requestIdentity,
  );
  if (identityAttempt && identityAttempt.attemptRunId !== successor?.attemptRunId) {
    throw gatewayError("clarification_retry_conflict");
  }
  const attemptNumber = successor?.attemptNumber ?? priorAttempt.attemptNumber + 1;
  const attemptRunId = successor?.attemptRunId ?? `run-${crypto.randomUUID()}`;
  const normalizedDispatch = clarificationDispatchInput({
    producerKind: "clarification_retry",
    scopeRef: resolution.resolutionId,
    requestIdentity,
    threadId: resolution.threadId,
    actorId,
    answer: resolution.submission.answer,
    resolutionId: resolution.resolutionId,
    sourceRunId: resolution.sourceRunId,
    attemptRunId,
    previousAttemptRunId: failedRunId,
    selectedOptionId: resolution.submission.selectedOptionId,
    retryAttempt: true,
  });
  const requestDigest = runDispatchRequestDigest(normalizedDispatch);
  if (successor) {
    if (
      successor.requestIdentity !== requestIdentity
      || successor.requestDigest !== requestDigest
    ) {
      throw gatewayError("clarification_retry_conflict");
    }
    return clarificationExecutionClaimFromMemory(
      store,
      resolution,
      successor,
      true,
      null,
    );
  }

  const createdAt = new Date().toISOString();
  const run: RunRecord = {
    id: attemptRunId,
    threadId: resolution.threadId,
    topicId: resolution.topicId,
    status: "queued",
    createdAt,
    request: clarificationAttemptRunRequest({
      resolutionId: resolution.resolutionId,
      sourceRunId: resolution.sourceRunId,
      attemptNumber,
      previousAttemptRunId: failedRunId,
      retryAttempt: true,
    }),
  };
  const attempt: ClarificationExecutionAttemptRecord = {
    attemptRunId,
    resolutionId: resolution.resolutionId,
    previousAttemptRunId: failedRunId,
    attemptNumber,
    requestIdentity,
    requestDigest,
    createdAt,
  };
  const dispatch: RunDispatchRecord = {
    dispatchId: `dispatch-${crypto.randomUUID()}`,
    producerKind: "clarification_retry",
    scopeRef: resolution.resolutionId,
    requestIdentity,
    requestDigest,
    requestPayload: normalizedDispatch.requestPayload,
    threadId: resolution.threadId,
    runId: attemptRunId,
    messageId: null,
    state: "pending",
    ownerId: null,
    leaseEpoch: 0,
    leaseExpiresAt: null,
    heartbeatAt: null,
    terminalStatus: null,
    failureReason: null,
  };
  store.runs.set(attemptRunId, run);
  store.clarificationExecutionAttempts.set(attemptRunId, attempt);
  store.runDispatches.set(attemptRunId, dispatch);
  store.auditEvents.push(
    {
      eventType: "clarification_retry_queued",
      actorId,
      threadId: resolution.threadId,
      topicId: resolution.topicId,
      runId: attemptRunId,
      ref: failedRunId,
      payload: {
        resolutionId: resolution.resolutionId,
        previousAttemptRunId: failedRunId,
        attemptNumber,
      },
    },
    {
      eventType: "run_queued",
      actorId,
      threadId: resolution.threadId,
      topicId: resolution.topicId,
      runId: attemptRunId,
      ref: attemptRunId,
      payload: {
        dispatchId: dispatch.dispatchId,
        producerKind: dispatch.producerKind,
        resolutionId: resolution.resolutionId,
        previousAttemptRunId: failedRunId,
        attemptNumber,
      },
    },
  );
  return clarificationExecutionClaimFromMemory(
    store,
    resolution,
    attempt,
    false,
    null,
  );
}

export async function requireRun(runId: string, actorId: string): Promise<RunRecord> {
  actorId = normalizeActorId(actorId);
  if (conversationStoreMode() === "postgres") {
    const { rows } = await pool().query(
      `
      SELECT r.run_id, r.thread_id, r.status, r.created_at, t.owner_id
      FROM waje_runtime.analysis_runs r
      JOIN waje_runtime.investigation_threads t ON t.thread_id = r.thread_id
      WHERE r.run_id = $1
      `,
      [runId],
    );
    const row = rows[0];
    if (!row) throw gatewayError("run_not_found");
    if (row.owner_id !== actorId) throw gatewayError("run_owner_mismatch");
    return runRecordFromRow(row);
  }
  const run = memoryStore().runs.get(runId);
  if (!run) throw gatewayError("run_not_found");
  const thread = memoryStore().threads.get(run.threadId);
  if (!thread) throw gatewayError("thread_not_found");
  if (thread.ownerId !== actorId) throw gatewayError("run_owner_mismatch");
  return run;
}

export async function failQueuedRunDispatch(
  runId: string,
  failureReason: string = "agent_core_spawn_failed",
): Promise<RunRecord> {
  if (conversationStoreMode() === "postgres") {
    const client = await pool().connect();
    try {
      await client.query("BEGIN");
      const { rows } = await client.query(
        `
        UPDATE waje_runtime.analysis_runs
        SET status = 'failed',
            request = COALESCE(request, '{}'::jsonb)
              || jsonb_build_object('failure_reason', $2),
            updated_at = now()
        WHERE run_id = $1
          AND status = 'queued'
        RETURNING run_id, thread_id, status, request, created_at
        `,
        [runId, failureReason],
      );
      if (rows[0]) {
        await client.query(
          `
          INSERT INTO waje_runtime.audit_events(
            event_type, actor_id, thread_id, run_id, ref, payload
          ) VALUES
            ('run_status_changed', 'system', $1, $2, $2, $3::jsonb),
            ('run_dispatch_failed', 'system', $1, $2, $2, $4::jsonb)
          `,
          [
            rows[0].thread_id,
            runId,
            JSON.stringify({ status: "failed" }),
            JSON.stringify({ failureReason }),
          ],
        );
        await client.query("COMMIT");
        return runRecordFromRow(rows[0]);
      }
      const current = await client.query(
        `SELECT run_id, thread_id, status, request, created_at
         FROM waje_runtime.analysis_runs WHERE run_id = $1`,
        [runId],
      );
      await client.query("COMMIT");
      if (!current.rows[0]) throw gatewayError("run_not_found");
      return runRecordFromRow(current.rows[0]);
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  }
  const store = memoryStore();
  const current = store.runs.get(runId);
  if (!current) throw gatewayError("run_not_found");
  if (current.status !== "queued") return current;
  const failed: RunRecord = {
    ...current,
    status: "failed",
    request: { ...(current.request ?? {}), failure_reason: failureReason },
  };
  const stagedAudits = [
    ...store.auditEvents,
    {
      eventType: "run_status_changed",
      threadId: current.threadId,
      runId,
      ref: runId,
      payload: { status: "failed" },
    },
    {
      eventType: "run_dispatch_failed",
      threadId: current.threadId,
      runId,
      ref: runId,
      payload: { failureReason },
    },
  ];
  store.runs.set(runId, failed);
  store.auditEvents = stagedAudits;
  return failed;
}

export async function runEvents(runId: string, actorId: string): Promise<RunEvent[]> {
  const run = await requireRun(runId, actorId);
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
          answer_package: projectAnswerPackageForCustomer(packageRows.rows[0].payload ?? {}),
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

export async function runAuditTrace(runId: string, actorId: string): Promise<RunAuditTrace> {
  await requireRun(runId, actorId);
  if (conversationStoreMode() !== "postgres") {
    const run = await requireRun(runId, actorId);
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

export async function runRerunComparability(
  baseRunId: string,
  candidateRunId: string,
  actorId: string,
): Promise<RunRerunComparability> {
  const baseTrace = await runAuditTrace(baseRunId, actorId);
  const candidateTrace = await runAuditTrace(candidateRunId, actorId);
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

export async function addUserMessage(
  threadId: string,
  text: string,
  actorId: string,
): Promise<MessageRecord> {
  if (conversationStoreMode() === "postgres") {
    await requireThread(threadId, actorId);
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
    await audit("message_recorded", { actorId, threadId, ref: message.id });
    return message;
  }
  const thread = await requireThread(threadId, actorId);
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
  actorId: string,
): Promise<ArtifactRecord> {
  if (conversationStoreMode() === "postgres") {
    const { rows } = await pool().query(
      `
      SELECT a.artifact_id, a.thread_id, a.topic_id, a.snapshot_id,
             a.follow_up_context, a.created_at, t.owner_id
      FROM waje_runtime.investigation_artifacts a
      JOIN waje_runtime.investigation_threads t ON t.thread_id = a.thread_id
      WHERE a.artifact_id = $1
      `,
      [artifactId],
    );
    const row = rows[0];
    if (!row) throw gatewayError("artifact_not_found");
    if (row.owner_id !== actorId) throw gatewayError("artifact_owner_mismatch");
    await audit("artifact_continue_allowed", {
      actorId,
      threadId: row.thread_id,
      topicId: row.topic_id,
      ref: artifactId,
    });
    return {
      id: row.artifact_id,
      threadId: row.thread_id,
      topicId: row.topic_id,
      snapshotId: row.snapshot_id,
      followUpContext: row.follow_up_context,
      createdAt: row.created_at,
    } satisfies ArtifactRecord;
  }
  const artifact = memoryStore().artifacts.get(artifactId);
  if (!artifact) throw gatewayError("artifact_not_found");
  const thread = memoryStore().threads.get(artifact.threadId);
  if (!thread) throw gatewayError("thread_not_found");
  if (thread.ownerId !== actorId) throw gatewayError("artifact_owner_mismatch");
  memoryStore().auditEvents.push({
    eventType: "artifact_continue_allowed",
    actorId,
    threadId: artifact.threadId,
    topicId: artifact.topicId,
    ref: artifactId,
  });
  return artifact;
}

export async function readArtifact(
  artifactId: string,
  actorId: string,
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
        a.follow_up_context,
        a.created_at,
        t.owner_id,
        COALESCE(NULLIF(a.payload, '{}'::jsonb), p.payload, '{}'::jsonb) AS answer_package
      FROM waje_runtime.investigation_artifacts a
      JOIN waje_runtime.investigation_threads t ON t.thread_id = a.thread_id
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
    if (!row) throw gatewayError("artifact_not_found");
    if (row.owner_id !== actorId) throw gatewayError("artifact_owner_mismatch");
    const answerPackage = projectAnswerPackageForCustomer(row.answer_package ?? {});
    const visibleSectionIds = visibleSections(answerPackage);
    await audit(action === "export" ? "artifact_exported" : "artifact_opened", {
      actorId,
      threadId: row.thread_id,
      topicId: row.topic_id,
      ref: artifactId,
      payload: { visibleSectionIds },
    });
    return {
      id: row.artifact_id,
      threadId: row.thread_id,
      topicId: row.topic_id,
      snapshotId: row.snapshot_id,
      followUpContext: row.follow_up_context,
      createdAt: row.created_at,
      visibleSectionIds,
      hiddenSectionCount: hiddenSectionCount(row.answer_package ?? {}, answerPackage),
      answerPackage,
    } satisfies VisibleArtifactRecord;
  }
  const artifact = memoryStore().artifacts.get(artifactId);
  if (!artifact) throw gatewayError("artifact_not_found");
  const thread = memoryStore().threads.get(artifact.threadId);
  if (!thread) throw gatewayError("thread_not_found");
  if (thread.ownerId !== actorId) throw gatewayError("artifact_owner_mismatch");
  memoryStore().auditEvents.push({
    eventType: action === "export" ? "artifact_exported" : "artifact_opened",
    actorId,
    threadId: artifact.threadId,
    topicId: artifact.topicId,
    ref: artifactId,
    payload: { visibleSectionIds: [] },
  });
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
  actorId: string,
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
  const run = await requireRun(payload.runId, actorId);
  if (conversationStoreMode() === "postgres") {
    await audit("clarification_answer_recorded", {
      actorId,
      threadId: run.threadId,
      runId: payload.runId,
      ref: payload.runId,
      payload,
    });
  }
  return { ...payload, threadId: run.threadId, status: "accepted" };
}

export async function createMemoryProposal(
  threadId: string,
  text: string,
  actorId: string,
): Promise<MemoryProposalRecord> {
  await requireThread(threadId, actorId);
  if (conversationStoreMode() === "postgres") {
    const proposal: MemoryProposalRecord = {
      id: `memory-proposal-${crypto.randomUUID()}`,
      threadId,
      ownerId: actorId,
      text,
      status: "proposed",
      createdAt: new Date().toISOString(),
    };
    await pool().query(
      `
      INSERT INTO waje_runtime.memory_proposals(
        proposal_id, thread_id, text, source_ref, owner_id, status
      )
      VALUES ($1, $2, $3, $4, $5, $6)
      `,
      [proposal.id, threadId, text, proposal.id, actorId, proposal.status],
    );
    await audit("memory_proposal_recorded", {
      actorId,
      threadId,
      ref: proposal.id,
    });
    return proposal;
  }
  const proposal: MemoryProposalRecord = {
    id: `memory-proposal-${crypto.randomUUID()}`,
    threadId,
    ownerId: actorId,
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
  actorId: string,
): Promise<MemoryProposalRecord> {
  if (conversationStoreMode() === "postgres") {
    const proposalRows = await pool().query(
      `
      SELECT p.proposal_id, p.thread_id, p.owner_id, t.owner_id AS thread_owner_id
      FROM waje_runtime.memory_proposals p
      JOIN waje_runtime.investigation_threads t ON t.thread_id = p.thread_id
      WHERE p.proposal_id = $1
      `,
      [proposalId],
    );
    const existing = proposalRows.rows[0];
    if (!existing) throw gatewayError("memory_proposal_not_found");
    if (existing.owner_id !== actorId || existing.thread_owner_id !== actorId) {
      throw gatewayError("memory_owner_mismatch");
    }
    const { rows } = await pool().query(
      `
      UPDATE waje_runtime.memory_proposals
      SET status = $2, decided_at = now()
      WHERE proposal_id = $1
      RETURNING proposal_id, thread_id, owner_id, text, status, created_at
      `,
      [proposalId, status],
    );
    const row = rows[0];
    if (!row) throw gatewayError("memory_proposal_not_found");
    await audit(`memory_proposal_${status}`, {
      actorId,
      threadId: row.thread_id,
      ref: proposalId,
    });
    return {
      id: row.proposal_id,
      threadId: row.thread_id,
      ownerId: row.owner_id,
      text: row.text,
      status: row.status,
      createdAt: row.created_at,
    } satisfies MemoryProposalRecord;
  }
  const proposal = memoryStore().memoryProposals.get(proposalId);
  if (!proposal) throw gatewayError("memory_proposal_not_found");
  if (proposal.ownerId !== actorId) throw gatewayError("memory_owner_mismatch");
  await requireThread(proposal.threadId, actorId);
  proposal.status = status;
  return proposal;
}

export function jsonError(error: unknown, status?: number) {
  const code = error instanceof GatewayRuntimeError
    ? error.code
    : error instanceof Error
      ? error.message
      : "unknown_error";
  return Response.json(
    { error: code },
    {
      status: status
        ?? (error instanceof GatewayRuntimeError
          ? error.httpStatus
          : gatewayHttpStatus(code)),
    },
  );
}

function gatewayHttpStatus(code: string) {
  if (code === "customer_identity_required") return 401;
  if (code === "internal_route_unavailable") return 404;
  if (code.endsWith("_not_found")) {
    return 404;
  }
  if (
    [
      "clarification_source_not_waiting",
      "clarification_resolution_conflict",
      "clarification_resolution_authority_mismatch",
      "clarification_resolution_not_accepted",
      "clarification_resolution_source_request_invalid",
      "clarification_resolution_source_choices_missing",
      "clarification_source_topic_invalid",
      "clarification_resolution_attempt_invalid",
      "clarification_retry_conflict",
      "clarification_retry_source_not_failed",
      "run_dispatch_conflict",
      "run_dispatch_lease_lost",
    ].includes(code)
  ) {
    return 409;
  }
  if (code === "artifact_thread_mismatch") {
    return 409;
  }
  if (
    code === "thread_owner_mismatch"
    || code === "run_owner_mismatch"
    || code === "artifact_owner_mismatch"
    || code === "memory_owner_mismatch"
  ) {
    return 403;
  }
  if (
    code === "clarification_answer_required"
    || code.startsWith("clarification_submission_")
    || code === "clarification_selected_option_invalid"
    || code === "clarification_resolution_choice_ambiguous"
    || code === "clarification_retry_run_invalid"
    || code.startsWith("run_dispatch_request_identity_")
    || code === "run_dispatch_request_invalid"
    || code === "run_dispatch_producer_invalid"
    || code === "thread_owner_input_forbidden"
    || code === "customer_identity_invalid"
  ) {
    return 400;
  }
  if (
    code === "agent_core_run_id_mismatch"
    || code === "agent_core_process_failed"
    || code.startsWith("agent_core_output_")
  ) {
    return 502;
  }
  if (["agent_core_spawn_failed", "agent_core_startup_failed"].includes(code)) {
    return 503;
  }
  return 500;
}

function normalizeRunDispatchInput(input: {
  producerKind: RunDispatchRecord["producerKind"];
  scopeRef: string;
  requestIdentity: string;
  threadId: string;
  text: string;
  actorId: string;
  requestPayload?: Record<string, unknown>;
}) {
  if (![
    "thread_message",
    "artifact_continue",
    "clarification_resume",
    "clarification_retry",
  ].includes(input.producerKind)) {
    throw gatewayError("run_dispatch_producer_invalid");
  }
  if (
    !isNonEmptyGatewayString(input.scopeRef)
    || !isNonEmptyGatewayString(input.requestIdentity)
    || !isNonEmptyGatewayString(input.threadId)
    || !isNonEmptyGatewayString(input.text)
    || !isNonEmptyGatewayString(input.actorId)
  ) {
    throw gatewayError("run_dispatch_request_invalid");
  }
  const requestPayload = canonicalGatewayRecord(input.requestPayload ?? {});
  const actorId = normalizeActorId(input.actorId);
  return {
    producerKind: input.producerKind,
    scopeRef: input.scopeRef.trim(),
    requestIdentity: input.requestIdentity.trim(),
    threadId: input.threadId.trim(),
    text: input.text.trim(),
    actorId,
    requestPayload,
  };
}

function runDispatchRequestDigest(input: ReturnType<typeof normalizeRunDispatchInput>) {
  return createHash("sha256").update(JSON.stringify(canonicalGatewayValue({
    producerKind: input.producerKind,
    scopeRef: input.scopeRef,
    threadId: input.threadId,
    text: input.text,
    requestPayload: input.requestPayload,
  }))).digest("hex");
}

function runDispatchIdentityLock(input: ReturnType<typeof normalizeRunDispatchInput>) {
  return JSON.stringify([
    input.producerKind,
    input.scopeRef,
    input.requestIdentity,
  ]);
}

function canonicalGatewayRecord(value: Record<string, unknown>) {
  return canonicalGatewayValue(value) as Record<string, unknown>;
}

function canonicalGatewayValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalGatewayValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, canonicalGatewayValue(item)]),
    );
  }
  if (["string", "number", "boolean"].includes(typeof value) || value === null) {
    return value;
  }
  throw gatewayError("run_dispatch_request_invalid");
}

function gatewayValueDigest(value: unknown) {
  return createHash("sha256")
    .update(JSON.stringify(canonicalGatewayValue(value)))
    .digest("hex");
}

function runDispatchState(value: unknown): RunDispatchRecord["state"] {
  if (["pending", "leased", "running", "terminal"].includes(String(value))) {
    return String(value) as RunDispatchRecord["state"];
  }
  throw gatewayError("run_dispatch_state_invalid");
}

function runDispatchClaimFromRow(
  row: Record<string, unknown>,
  replayed: boolean,
): RunDispatchClaim {
  const message: MessageRecord = {
    id: String(row.message_id),
    role: "user",
    text: String(row.text ?? ""),
    createdAt: String(row.message_created_at),
  };
  const run: RunRecord = {
    id: String(row.run_id),
    threadId: String(row.thread_id),
    status: validatedRunStatus(row.status),
    createdAt: String(row.run_created_at),
    request: (row.request as Record<string, unknown>) ?? {},
  };
  return {
    message,
    run,
    dispatch: {
      dispatchId: String(row.dispatch_id),
      producerKind: String(row.producer_kind) as RunDispatchRecord["producerKind"],
      scopeRef: String(row.scope_ref),
      requestIdentity: String(row.request_identity),
      requestDigest: String(row.request_digest),
      requestPayload: canonicalGatewayRecord(
        (row.request_payload as Record<string, unknown>) ?? {},
      ),
      threadId: String(row.thread_id),
      runId: String(row.run_id),
      messageId: String(row.message_id),
      state: runDispatchState(row.dispatch_state),
      ownerId: typeof row.owner_id === "string" ? row.owner_id : null,
      leaseEpoch: Number(row.lease_epoch ?? 0),
      leaseExpiresAt: row.lease_expires_at ? String(row.lease_expires_at) : null,
      heartbeatAt: row.heartbeat_at ? String(row.heartbeat_at) : null,
      terminalStatus: typeof row.terminal_status === "string" ? row.terminal_status : null,
      failureReason: typeof row.failure_reason === "string" ? row.failure_reason : null,
    },
    replayed,
  };
}

function runDispatchLeaseMs() {
  const configured = Number(process.env.WAJE_RUN_DISPATCH_LEASE_MS ?? "30000");
  return Number.isFinite(configured) && configured > 0 ? configured : 30000;
}

function normalizeClarificationResolutionInput(input: {
  sourceRunId: unknown;
  requestIdentity: unknown;
  answer: unknown;
  selectedOptionId?: unknown;
  source?: unknown;
}) {
  if (!isNonEmptyGatewayString(input.sourceRunId)) {
    throw gatewayError("clarification_submission_source_run_invalid");
  }
  if (
    !isNonEmptyGatewayString(input.requestIdentity)
    || input.requestIdentity.trim().length > 256
  ) {
    throw gatewayError("run_dispatch_request_identity_invalid");
  }
  if (!isNonEmptyGatewayString(input.answer)) {
    throw gatewayError("clarification_answer_required");
  }
  if (
    input.selectedOptionId !== undefined
    && input.selectedOptionId !== null
    && !isNonEmptyGatewayString(input.selectedOptionId)
  ) {
    throw gatewayError("clarification_selected_option_invalid");
  }
  if (input.source !== undefined && input.source !== "user") {
    throw gatewayError("clarification_submission_source_invalid");
  }
  return {
    sourceRunId: input.sourceRunId.trim(),
    requestIdentity: input.requestIdentity.trim(),
    answer: input.answer.trim(),
    selectedOptionId: typeof input.selectedOptionId === "string"
      ? input.selectedOptionId.trim()
      : null,
    source: "user" as const,
  };
}

function isNonEmptyGatewayString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function normalizeActorId(actorId: string) {
  if (
    !isNonEmptyGatewayString(actorId)
    || actorId.trim().length > 256
    || /[\u0000-\u001f\u007f]/.test(actorId)
  ) {
    throw gatewayError("customer_identity_invalid");
  }
  return actorId.trim();
}

function normalizedGatewayIdentifier(
  value: unknown,
  errorCode: string,
  maxLength = 512,
) {
  if (
    !isNonEmptyGatewayString(value)
    || value.trim().length > maxLength
    || /[\u0000-\u001f\u007f]/.test(value)
  ) {
    throw gatewayError(errorCode);
  }
  return value.trim();
}

function clarificationSourceActions(sourceRequest: unknown) {
  if (!isGatewayRecord(sourceRequest)) {
    throw gatewayError("clarification_resolution_source_request_invalid");
  }
  const envelope = sourceRequest.clarification_source_envelope;
  const clarification = isGatewayRecord(envelope)
    ? envelope.clarification
    : null;
  const actions = isGatewayRecord(clarification)
    && Array.isArray(clarification.choice_actions)
    ? clarification.choice_actions
        .filter(isGatewayRecord)
        .map((action) => canonicalGatewayRecord(action))
    : [];
  if (!actions.length) {
    throw gatewayError("clarification_resolution_source_choices_missing");
  }
  return actions;
}

function clarificationBusinessLabel(value: unknown) {
  let label = String(value ?? "").trim();
  while (label.endsWith("（推荐）")) {
    label = label.slice(0, -"（推荐）".length).trimEnd();
  }
  return label.replace(/。$/, "");
}

function resolveAcceptedClarificationChoice(
  sourceRequest: unknown,
  answer: string,
  selectedOptionId: string | null,
) {
  const actions = clarificationSourceActions(sourceRequest);
  let matches: Record<string, unknown>[];
  if (selectedOptionId) {
    matches = actions.filter(
      (action) => String(action.choice_id ?? "") === selectedOptionId,
    );
  } else {
    const normalizedAnswer = clarificationBusinessLabel(answer);
    matches = actions.filter((action) => clarificationBusinessLabel(
      action.business_label ?? action.business_semantics,
    ) === normalizedAnswer);
    if (!matches.length) {
      matches = actions.filter(
        (action) => String(action.action_kind ?? "") === "user_redirect",
      );
    }
  }
  if (matches.length !== 1) {
    throw gatewayError("clarification_resolution_choice_ambiguous");
  }
  return matches[0];
}

function clarificationResolutionDigest(input: {
  resolutionId: string;
  sourceRunId: string;
  threadId: string;
  topicId: string;
  ownerId: string;
  submission: Record<string, unknown>;
  acceptedChoice: Record<string, unknown>;
  messageId: string;
  sourceRequestDigest: string;
}) {
  return gatewayValueDigest({
    schema_version: "clarification-resolution.v1",
    resolution_id: normalizedGatewayIdentifier(
      input.resolutionId,
      "clarification_resolution_digest_material_invalid",
    ),
    source_run_id: normalizedGatewayIdentifier(
      input.sourceRunId,
      "clarification_resolution_digest_material_invalid",
    ),
    thread_id: normalizedGatewayIdentifier(
      input.threadId,
      "clarification_resolution_digest_material_invalid",
    ),
    topic_id: normalizedGatewayIdentifier(
      input.topicId,
      "clarification_resolution_digest_material_invalid",
    ),
    owner_id: normalizedGatewayIdentifier(
      input.ownerId,
      "clarification_resolution_digest_material_invalid",
    ),
    message_id: normalizedGatewayIdentifier(
      input.messageId,
      "clarification_resolution_digest_material_invalid",
    ),
    source_request_digest: normalizedGatewayIdentifier(
      input.sourceRequestDigest,
      "clarification_resolution_digest_material_invalid",
    ),
    submission: input.submission,
    accepted_choice: input.acceptedChoice,
  });
}

function clarificationDispatchInput(input: {
  producerKind: "clarification_resume" | "clarification_retry";
  scopeRef: string;
  requestIdentity: string;
  threadId: string;
  actorId: string;
  answer: string;
  resolutionId: string;
  sourceRunId: string;
  attemptRunId: string;
  previousAttemptRunId: string | null;
  selectedOptionId: string | null;
  retryAttempt?: boolean;
}) {
  const requestPayload: Record<string, unknown> = {
    sourceRunId: input.sourceRunId,
    resolutionId: input.resolutionId,
    attemptRunId: input.attemptRunId,
    answer: input.answer,
    selectedOptionId: input.selectedOptionId,
    source: "user",
    retryAttempt: input.retryAttempt === true,
  };
  if (input.retryAttempt === true) {
    requestPayload.previousAttemptRunId = normalizedGatewayIdentifier(
      input.previousAttemptRunId,
      "clarification_retry_run_invalid",
    );
  } else if (input.previousAttemptRunId !== null) {
    throw gatewayError("clarification_resolution_attempt_invalid");
  }
  return normalizeRunDispatchInput({
    producerKind: input.producerKind,
    scopeRef: input.scopeRef,
    requestIdentity: input.requestIdentity,
    threadId: input.threadId,
    text: input.answer,
    actorId: input.actorId,
    requestPayload,
  });
}

function clarificationAttemptRunRequest(input: {
  resolutionId: string;
  sourceRunId: string;
  attemptNumber: number;
  previousAttemptRunId: string | null;
  retryAttempt?: boolean;
}) {
  return {
    clarification_resolution_id: input.resolutionId,
    clarification_source_run_id: input.sourceRunId,
    clarification_attempt_number: input.attemptNumber,
    clarification_previous_attempt_run_id: input.previousAttemptRunId,
    clarification_retry_attempt: input.retryAttempt === true,
  };
}

function expectedResolutionDigestFromRecord(
  resolution: ClarificationResolutionRecord,
) {
  return clarificationResolutionDigest({
    resolutionId: resolution.resolutionId,
    sourceRunId: resolution.sourceRunId,
    threadId: resolution.threadId,
    topicId: resolution.topicId,
    ownerId: resolution.ownerId,
    submission: resolution.submission,
    acceptedChoice: resolution.acceptedChoice,
    messageId: resolution.messageId,
    sourceRequestDigest: resolution.sourceRequestDigest,
  });
}

function assertRetryResolutionAuthority(
  row: Record<string, unknown>,
  actorId: string,
) {
  if (
    String(row.status ?? "") !== "accepted"
    || !row.accepted_at
  ) {
    throw gatewayError("clarification_resolution_not_accepted");
  }
  if (String(row.previous_attempt_status ?? "") !== "failed") {
    throw gatewayError("clarification_retry_source_not_failed");
  }
  if (row.owner_id !== actorId || row.current_owner_id !== actorId) {
    throw gatewayError("run_owner_mismatch");
  }
  if (
    row.thread_id !== row.source_thread_id
    || row.thread_id !== row.previous_thread_id
    || row.topic_id !== row.source_topic_id
    || row.topic_id !== row.previous_topic_id
  ) {
    throw gatewayError("clarification_resolution_authority_mismatch");
  }
  const sourceRequest = canonicalGatewayRecord(
    (row.source_request as Record<string, unknown>) ?? {},
  );
  const sourceRequestDigest = gatewayValueDigest(sourceRequest);
  const submission = canonicalGatewayRecord(
    (row.submission as Record<string, unknown>) ?? {},
  );
  const acceptedChoice = resolveAcceptedClarificationChoice(
    sourceRequest,
    String(submission.answer ?? ""),
    typeof submission.selectedOptionId === "string"
      ? submission.selectedOptionId
      : null,
  );
  const expectedDigest = clarificationResolutionDigest({
    resolutionId: String(row.resolution_id),
    sourceRunId: String(row.source_run_id),
    threadId: String(row.thread_id),
    topicId: String(row.topic_id),
    ownerId: String(row.owner_id),
    submission,
    acceptedChoice,
    messageId: String(row.message_id),
    sourceRequestDigest,
  });
  if (
    row.source_request_digest !== sourceRequestDigest
    || row.resolution_digest !== expectedDigest
    || gatewayValueDigest(row.accepted_choice ?? {})
      !== gatewayValueDigest(acceptedChoice)
  ) {
    throw gatewayError("clarification_resolution_authority_mismatch");
  }
}

function assertMemoryRetryResolutionAuthority(
  store: MemoryStore,
  resolution: ClarificationResolutionRecord,
  failedRun: RunRecord,
  actorId: string,
) {
  if (
    resolution.status !== "accepted"
    || !isNonEmptyGatewayString(resolution.acceptedAt)
  ) {
    throw gatewayError("clarification_resolution_not_accepted");
  }
  if (failedRun.status !== "failed") {
    throw gatewayError("clarification_retry_source_not_failed");
  }
  const sourceRun = store.runs.get(resolution.sourceRunId);
  const thread = store.threads.get(resolution.threadId);
  if (
    resolution.ownerId !== actorId
    || (thread && thread.ownerId !== actorId)
  ) {
    throw gatewayError("run_owner_mismatch");
  }
  if (
    !sourceRun
    || !thread
    || sourceRun.threadId !== resolution.threadId
    || sourceRun.topicId !== resolution.topicId
    || failedRun.threadId !== resolution.threadId
    || failedRun.topicId !== resolution.topicId
  ) {
    throw gatewayError("clarification_resolution_authority_mismatch");
  }
  const sourceRequestDigest = gatewayValueDigest(sourceRun.request ?? {});
  const acceptedChoice = resolveAcceptedClarificationChoice(
    sourceRun.request,
    resolution.submission.answer,
    resolution.submission.selectedOptionId,
  );
  if (
    resolution.sourceRequestDigest !== sourceRequestDigest
    || resolution.resolutionDigest !== expectedResolutionDigestFromRecord(resolution)
    || gatewayValueDigest(resolution.acceptedChoice)
      !== gatewayValueDigest(acceptedChoice)
  ) {
    throw gatewayError("clarification_resolution_authority_mismatch");
  }
}

function clarificationExecutionClaimFromRow(
  row: Record<string, unknown>,
  replayed: boolean,
  messageOverride?: MessageRecord | null,
): ClarificationExecutionClaim {
  const submission = canonicalGatewayRecord(
    (row.submission as Record<string, unknown>) ?? {},
  );
  const attemptRunId = String(row.attempt_run_id);
  const message = messageOverride === undefined
    ? {
        id: String(row.message_id),
        role: "user" as const,
        text: String(row.text ?? submission.answer ?? ""),
        createdAt: String(row.message_created_at),
      }
    : messageOverride;
  return {
    sourceRunId: String(row.source_run_id),
    resolutionId: String(row.resolution_id),
    attemptRunId,
    previousAttemptRunId: typeof row.previous_attempt_run_id === "string"
      ? row.previous_attempt_run_id
      : null,
    attemptNumber: Number(row.attempt_number),
    threadId: String(row.thread_id),
    topicId: String(row.topic_id),
    ownerId: String(row.owner_id),
    requestIdentity: String(row.request_identity),
    answer: String(submission.answer ?? ""),
    selectedOptionId: typeof submission.selectedOptionId === "string"
      ? submission.selectedOptionId
      : null,
    source: "user",
    acceptedChoice: canonicalGatewayRecord(
      (row.accepted_choice as Record<string, unknown>) ?? {},
    ),
    message,
    run: {
      id: attemptRunId,
      threadId: String(row.thread_id),
      topicId: String(row.topic_id),
      status: validatedRunStatus(row.attempt_status),
      createdAt: String(row.run_created_at),
      request: (row.attempt_request as Record<string, unknown>) ?? {},
    },
    replayed,
  };
}

function clarificationExecutionClaimFromMemory(
  store: MemoryStore,
  resolution: ClarificationResolutionRecord,
  attempt: ClarificationExecutionAttemptRecord,
  replayed: boolean,
  messageOverride?: MessageRecord | null,
): ClarificationExecutionClaim {
  const run = store.runs.get(attempt.attemptRunId);
  if (!run) throw gatewayError("clarification_attempt_not_found");
  const thread = store.threads.get(resolution.threadId);
  const message = messageOverride === undefined
    ? thread?.messages.find((item) => item.id === resolution.messageId) ?? null
    : messageOverride;
  if (messageOverride === undefined && !message) {
    throw gatewayError("clarification_resolution_message_not_found");
  }
  return {
    sourceRunId: resolution.sourceRunId,
    resolutionId: resolution.resolutionId,
    attemptRunId: attempt.attemptRunId,
    previousAttemptRunId: attempt.previousAttemptRunId,
    attemptNumber: attempt.attemptNumber,
    threadId: resolution.threadId,
    topicId: resolution.topicId,
    ownerId: resolution.ownerId,
    requestIdentity: attempt.requestIdentity,
    answer: resolution.submission.answer,
    selectedOptionId: resolution.submission.selectedOptionId,
    source: "user",
    acceptedChoice: resolution.acceptedChoice,
    message,
    run,
    replayed,
  };
}

function memoryStore() {
  globalStore.__wajeConversationMemoryStore ??= {
    threads: new Map(),
    runs: new Map(),
    artifacts: new Map(),
    memoryProposals: new Map(),
    clarificationResolutions: new Map(),
    clarificationExecutionAttempts: new Map(),
    runDispatches: new Map(),
    auditEvents: [],
  };
  const store = globalStore.__wajeConversationMemoryStore;
  store.clarificationResolutions ??= new Map();
  store.clarificationExecutionAttempts ??= new Map();
  store.runDispatches ??= new Map();
  store.auditEvents ??= [];
  return store;
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
  fields: {
    actorId?: string;
    threadId?: string;
    topicId?: string;
    runId?: string;
    ref?: string;
    payload?: unknown;
  },
) {
  await pool().query(
    `
    INSERT INTO waje_runtime.audit_events(
      event_type, actor_id, thread_id, topic_id, run_id, ref, payload
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
    `,
    [
      eventType,
      fields.actorId ?? "system",
      fields.threadId ?? null,
      fields.topicId ?? null,
      fields.runId ?? null,
      fields.ref ?? null,
      JSON.stringify(fields.payload ?? {}),
    ],
  );
}

export function projectAnswerPackageForCustomer(answerPackage: Record<string, unknown>) {
  const allowed = customerVisibleSections();
  const sections = Array.isArray(answerPackage.sections)
    ? answerPackage.sections.filter((section) => {
        if (!section || typeof section !== "object") return false;
        return allowed.has(String((section as Record<string, unknown>).visibility ?? ""));
      }).map((section) => projectSectionPayloadForCustomer(section as Record<string, unknown>, allowed))
    : [];
  return {
    run_id: answerPackage.run_id,
    status: answerPackage.status,
    package_type: answerPackage.package_type,
    sections,
  };
}

export function projectAgentCoreForCustomer(agentCore: Record<string, unknown>) {
  const resumed = isGatewayRecord(agentCore.result)
    ? agentCore.result
    : {};
  const status = String(agentCore.status ?? "");
  const innerStatus = businessString(resumed.status);
  if (agentCoreStatusMismatch(status, innerStatus)) {
    return filterAgentCoreFailure(agentCore, resumed, true);
  }
  const visibleResult = {
    run_id: resumed.run_id,
    turn_id: resumed.turn_id,
    topic_id: resumed.topic_id,
    status,
  };

  if (status === "completed") {
    const answerPackage = isGatewayRecord(resumed.answer_package)
      ? projectAnswerPackageForCustomer(resumed.answer_package)
      : null;
    return {
      status,
      result: { ...visibleResult, answer_package: answerPackage },
    };
  }
  if (status === "waiting_for_clarification") {
    return {
      status,
      result: {
        ...visibleResult,
        clarification: filterBusinessClarification(resumed.clarification),
      },
    };
  }
  if (status === "failed") {
    return filterAgentCoreFailure(agentCore, resumed, false);
  }
  if (status === "completed_without_workflow") {
    return {
      status,
      result: {
        ...visibleResult,
        intent: businessString(resumed.intent),
        topic_relation: businessString(resumed.topic_relation),
      },
    };
  }
  return { status, result: visibleResult };
}

function agentCoreStatusMismatch(wrapperStatus: string, innerStatus: string | undefined) {
  if (wrapperStatus === "failed") {
    return innerStatus !== undefined && innerStatus !== wrapperStatus;
  }
  if (
    wrapperStatus === "completed"
    || wrapperStatus === "completed_without_workflow"
    || wrapperStatus === "waiting_for_clarification"
  ) {
    return innerStatus !== wrapperStatus;
  }
  return false;
}

function filterAgentCoreFailure(
  agentCore: Record<string, unknown>,
  resumed: Record<string, unknown>,
  statusMismatch: boolean,
) {
  const reviewedError = reviewedAgentCoreError(agentCore.error);
  const error = statusMismatch
    ? reviewedError ?? "agent_core_run_failed"
    : reviewedError;
  return {
    status: "failed",
    ...(error ? { error } : {}),
    result: {
      run_id: resumed.run_id,
      turn_id: resumed.turn_id,
      topic_id: resumed.topic_id,
      status: "failed",
      failure_reason: statusMismatch
        ? "agent_core_run_failed"
        : reviewedAgentCoreFailureReason(resumed.failure_reason),
    },
  };
}

function filterBusinessClarification(value: unknown) {
  if (!isGatewayRecord(value)) return null;
  return {
    clarification_id: businessString(value.clarification_id),
    reason: businessString(value.reason),
    status: businessString(value.status),
    allow_freeform: typeof value.allow_freeform === "boolean"
      ? value.allow_freeform
      : undefined,
    questions: Array.isArray(value.questions)
      ? value.questions.flatMap((question) => {
          if (!isGatewayRecord(question)) return [];
          return [{
            question_id: businessString(question.question_id),
            question: businessString(question.question),
            options: Array.isArray(question.options)
              ? question.options.flatMap(filterBusinessClarificationOption)
              : [],
          }];
        })
      : [],
    recommended_assumption: filterRecommendedAssumption(value.recommended_assumption),
    recommendation_reason: businessString(value.recommendation_reason),
  };
}

function filterBusinessClarificationOption(value: unknown): Record<string, unknown>[] {
  if (typeof value === "string") {
    return [{
      label: value,
      description: value,
      business_meaning: value,
    }];
  }
  if (!isGatewayRecord(value)) return [];
  return [{
    id: businessString(value.id) ?? businessString(value.option_id),
    label: businessString(value.label),
    description: businessString(value.description),
    recommended: typeof value.recommended === "boolean" ? value.recommended : undefined,
    business_meaning: businessString(value.business_meaning),
  }];
}

function filterRecommendedAssumption(value: unknown) {
  if (typeof value === "string") return value;
  if (!isGatewayRecord(value)) return undefined;
  return {
    option: businessString(value.option),
    assumption: businessString(value.assumption),
  };
}

function reviewedAgentCoreFailureReason(value: unknown) {
  const reason = businessString(value);
  if (reason && SAFE_AGENT_CORE_FAILURE_REASONS.has(reason)) return reason;
  return "agent_core_run_failed";
}

function reviewedAgentCoreError(value: unknown) {
  const error = businessString(value);
  if (!error) return undefined;
  if (SAFE_AGENT_CORE_ERRORS.has(error)) return error;
  return "agent_core_run_failed";
}

const SAFE_AGENT_CORE_FAILURE_REASONS = new Set([
  "analysis_delivery_persistence_failed",
  "material_authority_projection_failed",
  "analysis_runtime_bundle_validation_failed",
  "analysis_runtime_artifact_sync_failed",
  "analysis_runtime_store_commit_failed",
  "clarification_resume_authority_failed",
  "delivery_verifier_failed",
]);

const SAFE_AGENT_CORE_ERRORS = new Set([
  "agent_core_output_malformed_json",
  "agent_core_output_shape_invalid",
  "agent_core_output_status_invalid",
  "agent_core_process_failed",
  "agent_core_spawn_failed",
]);

function businessString(value: unknown) {
  return typeof value === "string" ? value : undefined;
}

function validatedRunStatus(value: unknown): RunStatus {
  if (typeof value === "string" && RUN_STATUSES.includes(value as RunStatus)) {
    return value as RunStatus;
  }
  throw new Error("analysis_run_status_invalid");
}

function runRecordFromRow(row: {
  run_id: string;
  thread_id: string;
  status: unknown;
  created_at: string;
  request?: Record<string, unknown>;
}): RunRecord {
  return {
    id: row.run_id,
    threadId: row.thread_id,
    status: validatedRunStatus(row.status),
    createdAt: row.created_at,
    ...(row.request ? { request: row.request } : {}),
  };
}

function isGatewayRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function customerVisibleSections() {
  return new Set(["business_summary", "aggregate_evidence", "diagnostic_detail"]);
}

function projectSectionPayloadForCustomer(section: Record<string, unknown>, allowed: Set<string>) {
  if (section.section_id !== "summary") return section;
  const payload = section.payload;
  if (!payload || typeof payload !== "object") return section;
  return {
    ...section,
    payload: projectSummaryPayloadForCustomer(
      payload as Record<string, unknown>,
      allowed,
      String(section.visibility ?? ""),
    ),
  };
}

function projectSummaryPayloadForCustomer(
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
      summary: "已跳过当前证据不足、个人资源归属不匹配、受限敏感源访问或不适合本轮问题的路径。",
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
