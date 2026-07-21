import { Pool, type PoolClient } from "pg";
import { createHash } from "crypto";

import {
  parseCustomerPublication,
  type CustomerPublication,
} from "./_customerPublicationContract";
import {
  projectCustomerAnalysisSnapshot,
  type CustomerAnalysisSnapshot,
  type CustomerMainStatus,
  type CustomerMessage,
  type CustomerPhase,
  type CustomerThreadSummary,
} from "./_customerAnalysisContract";

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
  "interaction_completed",
  "waiting_for_clarification",
  "planned",
  "evidence_ready",
  "authority_sealed",
  "narrative_ready",
  "completed",
  "failed",
] as const;

type RunStatus = (typeof RUN_STATUSES)[number];

const PLAN_RESULT_EVENT_STATUSES = new Set<RunStatus>([
  "planned",
  "evidence_ready",
  "authority_sealed",
  "narrative_ready",
  "completed",
]);

const EXECUTION_RESULT_EVENT_STATUSES = new Set<RunStatus>([
  "evidence_ready",
  "authority_sealed",
  "narrative_ready",
  "completed",
]);

type RunRecord = {
  id: string;
  threadId: string;
  topicId?: string | null;
  status: RunStatus;
  createdAt: string;
  request?: Record<string, unknown>;
};

type RunDispatchProducerKind =
  | "thread_message"
  | "clarification_resolution";

type RunDispatchRecord = {
  dispatchId: string;
  producerKind: RunDispatchProducerKind;
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
  dispatchId: string;
  acquired: boolean;
  ownerId: string | null;
  leaseEpoch: number;
  state: RunDispatchRecord["state"];
  reason: "acquired" | "active_lease" | "already_running" | "terminal" | "run_not_dispatchable";
  run: RunRecord;
};

type ThreadMessageDispatchInput = {
  producerKind: "thread_message";
  scopeRef: string;
  requestIdentity: string;
  threadId: string;
  text: string;
  actorId: string;
  requestPayload?: Record<string, unknown>;
  runId?: never;
};

type ClarificationResolutionDispatchInput = {
  producerKind: "clarification_resolution";
  scopeRef: string;
  requestIdentity: string;
  threadId: string;
  runId: string;
  text: string;
  actorId: string;
  requestPayload: Record<string, unknown>;
};

type RunDispatchInput =
  | ThreadMessageDispatchInput
  | ClarificationResolutionDispatchInput;


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
  customerPublication: CustomerPublication | null;
  publication: SafePublicationRefs | null;
  runNodes: Record<string, unknown>[];
  evidenceRefs: Record<string, unknown>[];
  resultRefs: Record<string, unknown>[];
  executionSnapshots: Record<string, unknown>[];
  auditEvents: Record<string, unknown>[];
  verifierStatus: {
    acceptedClaimCount: number;
    vetoedClaimCount: number;
    vetoedBlockCount: number;
    claimReportRefs: string[];
    blockReportRefs: string[];
  };
  traceCompleteness: {
    hasCustomerPublication: boolean;
    evidenceRefCount: number;
    resultRefCount: number;
    contractRefs: string[];
    snapshotRefs: string[];
    queryRefs: string[];
    resultRefs: string[];
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

export type SafePublicationRefs = {
  authority_bundle_ref: string;
  authority_bundle_digest: string;
  authority_sealed_at: string;
  publication_ref: string;
  publication_digest: string;
  published_at: string;
  projection_id: string;
  projection_digest: string;
  outbox_ref: string;
  delivery_status: string;
  delivery_attempted_at?: string;
};

export type SafePostExecutionState = {
  post_execution_status: string;
  analysis_status: "complete" | "boundary_only";
  publication_status: string;
  delivery_status: string;
  publication_refs: Record<string, string | null>;
  operational_failure?: {
    failure_ref: string;
    layer: "narrative" | "persistence";
    kind: string;
    retryability: "retryable" | "not_retryable";
    business_boundary: string;
  };
};

export type PersistedPublicationRun = {
  runId: string;
  threadId: string;
  runStatus: string;
  question: string;
  request: Record<string, unknown>;
  customerPublication: Record<string, unknown>;
  publication: SafePublicationRefs;
  runNodes: Record<string, unknown>[];
  workflowTransitions: Record<string, unknown>[];
  stageTimings: Record<string, unknown>[];
  evidenceRefs: Record<string, unknown>[];
  claimEvidenceLinks: Record<string, unknown>[];
  acceptedGraph: Record<string, unknown>[];
  verifierStatus: Record<string, unknown>;
  humanReview: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
};

export type PersistedRuntimeRun = {
  runId: string;
  threadId: string;
  runStatus: string;
  question: string;
  request: Record<string, unknown>;
  runNodes: Record<string, unknown>[];
  workflowTransitions: Record<string, unknown>[];
  stageTimings: Record<string, unknown>[];
  evidenceRefs: Record<string, unknown>[];
  acceptedGraph: Record<string, unknown>[];
  createdAt: string;
  updatedAt: string;
};

type ReadQueryClient = Pick<PoolClient, "query">;

export type PersistedAgentRunCandidates = {
  publicationRuns: PersistedPublicationRun[];
  runtimeRuns: PersistedRuntimeRun[];
};

type MemoryStore = {
  threads: Map<string, ThreadRecord>;
  runs: Map<string, RunRecord>;
  memoryProposals: Map<string, MemoryProposalRecord>;
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

export async function listCustomerThreadSummaries(
  actorId: string,
): Promise<CustomerThreadSummary[]> {
  actorId = normalizeActorId(actorId);
  if (conversationStoreMode() === "postgres") {
    const { rows } = await pool().query(
      `
      SELECT
        thread.thread_id,
        thread.created_at,
        COALESCE(first_message.text, '新分析') AS title,
        latest_run.status AS run_status,
        latest_run.request AS run_request,
        COALESCE(publication.limitation_count, 0) AS limitation_count,
        GREATEST(
          thread.updated_at,
          COALESCE(latest_run.updated_at, thread.updated_at),
          COALESCE(latest_message.created_at, thread.updated_at)
        ) AS updated_at
      FROM waje_runtime.investigation_threads thread
      LEFT JOIN LATERAL (
        SELECT message.text
        FROM waje_runtime.conversation_messages message
        WHERE message.thread_id = thread.thread_id
          AND message.role = 'user'
        ORDER BY message.created_at, message.message_id
        LIMIT 1
      ) first_message ON true
      LEFT JOIN LATERAL (
        SELECT message.created_at
        FROM waje_runtime.conversation_messages message
        WHERE message.thread_id = thread.thread_id
        ORDER BY message.created_at DESC, message.message_id DESC
        LIMIT 1
      ) latest_message ON true
      LEFT JOIN LATERAL (
        SELECT run.run_id, run.status, run.request, run.updated_at
        FROM waje_runtime.analysis_runs run
        WHERE run.thread_id = thread.thread_id
        ORDER BY run.created_at DESC, run.run_id DESC
        LIMIT 1
      ) latest_run ON true
      LEFT JOIN LATERAL (
        SELECT jsonb_array_length(customer.customer_payload -> 'limitation_refs')
          AS limitation_count
        FROM waje_runtime.publication_customer_payloads customer
        WHERE customer.run_attempt_id = latest_run.run_id
        ORDER BY customer.created_at DESC
        LIMIT 1
      ) publication ON true
      WHERE thread.owner_id = $1
      ORDER BY updated_at DESC, thread.thread_id DESC
      LIMIT 30
      `,
      [actorId],
    );
    return rows.map((row) => ({
      title: customerThreadTitle(String(row.title ?? "")),
      status: customerSummaryStatus(
        row.run_status,
        isGatewayRecord(row.run_request) ? row.run_request : {},
        Number(row.limitation_count ?? 0),
      ),
      updatedAt: new Date(row.updated_at).toISOString(),
      transport: { threadHandle: String(row.thread_id) },
    }));
  }
  return [...memoryStore().threads.values()]
    .filter((thread) => thread.ownerId === actorId)
    .map((thread) => {
      const runs = [...memoryStore().runs.values()]
        .filter((run) => run.threadId === thread.id)
        .sort((left, right) => right.createdAt.localeCompare(left.createdAt));
      const run = runs[0];
      return {
        title: customerThreadTitle(thread.messages[0]?.text ?? ""),
        status: customerSummaryStatus(run?.status, run?.request ?? {}, 0),
        updatedAt: run?.createdAt ?? thread.createdAt,
        transport: { threadHandle: thread.id },
      } satisfies CustomerThreadSummary;
    })
    .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
}

export async function loadCustomerAnalysisSnapshot(input: {
  threadId: string;
  actorId: string;
  runId?: string;
}): Promise<CustomerAnalysisSnapshot> {
  const thread = await requireThread(input.threadId, input.actorId);
  if (input.runId) {
    const requestedRun = await requireRun(input.runId, input.actorId);
    if (requestedRun.threadId !== thread.id) {
      throw gatewayError("run_thread_mismatch");
    }
  }
  if (conversationStoreMode() === "postgres") {
    const [messagesResult, publicationHistoryResult, runResult, versionResult] = await Promise.all([
      pool().query(
        `
        SELECT message_id, role, text, created_at
        FROM waje_runtime.conversation_messages
        WHERE thread_id = $1 AND role IN ('user', 'assistant')
        ORDER BY created_at, message_id
        `,
        [thread.id],
      ),
      pool().query(
        `
        SELECT history.run_id, history.customer_payload, history.created_at
        FROM (
          SELECT DISTINCT ON (customer.run_attempt_id)
            customer.run_attempt_id AS run_id,
            customer.customer_payload,
            customer.created_at
          FROM waje_runtime.publication_customer_payloads customer
          JOIN waje_runtime.analysis_runs run
            ON run.run_id = customer.run_attempt_id
          WHERE run.thread_id = $1
          ORDER BY customer.run_attempt_id, customer.created_at DESC
        ) history
        ORDER BY history.created_at, history.run_id
        `,
        [thread.id],
      ),
      input.runId
        ? pool().query(
            `
            SELECT run_id, status, request, created_at, updated_at
            FROM waje_runtime.analysis_runs
            WHERE thread_id = $1 AND run_id = $2
            LIMIT 1
            `,
            [thread.id, input.runId],
          )
        : pool().query(
            `
            SELECT run_id, status, request, created_at, updated_at
            FROM waje_runtime.analysis_runs
            WHERE thread_id = $1
            ORDER BY created_at DESC, run_id DESC
            LIMIT 1
            `,
            [thread.id],
          ),
      customerStateVersion(thread.id),
    ]);
    const row = runResult.rows[0];
    const persistedMessages: CustomerMessage[] = messagesResult.rows.map((message) => ({
      key: String(message.message_id),
      role: message.role === "assistant" ? "assistant" : "user",
      text: String(message.text),
      createdAt: new Date(message.created_at).toISOString(),
    }));
    const historicalAnswers: CustomerMessage[] = publicationHistoryResult.rows
      .filter((publicationRow) => !row || publicationRow.run_id !== row.run_id)
      .map((publicationRow) => {
        const publication = requireCustomerPublication(
          publicationRow.customer_payload,
        );
        return {
          key: `publication:${String(publicationRow.run_id)}`,
          role: "assistant" as const,
          text: publication.blocks.map((block) => block.text).join("\n\n"),
          createdAt: new Date(publicationRow.created_at).toISOString(),
        };
      });
    const messages = [...persistedMessages, ...historicalAnswers]
      .sort((left, right) => left.createdAt.localeCompare(right.createdAt));
    if (!row) {
      const version = versionResult.rows[0];
      return projectCustomerAnalysisSnapshot({
        thread: { id: thread.id, createdAt: thread.createdAt },
        messages,
        run: null,
        runNodes: [],
        currentClarification: null,
        interactionResult: null,
        customerPublication: null,
        acceptedOperationIds: [],
        confirmedAt: new Date(version.confirmed_at).toISOString(),
        stateVersion: String(version.state_version),
      });
    }
    const runId = String(row.run_id);
    const [
      nodesResult,
      clarificationResult,
      dispatchResult,
      publication,
      progressResult,
    ] = await Promise.all([
      pool().query(
        `
        SELECT node_name, status, COALESCE(finished_at, started_at) AS confirmed_at
        FROM waje_runtime.run_nodes
        WHERE run_id = $1
        ORDER BY finished_at NULLS LAST, started_at NULLS LAST, node_id
        `,
        [runId],
      ),
      pool().query(
        `
        SELECT payload, created_at
        FROM waje_runtime.audit_events
        WHERE run_id = $1
          AND event_type IN ('clarification_state_saved', 'clarification_requested')
        ORDER BY created_at DESC
        LIMIT 1
        `,
        [runId],
      ),
      pool().query(
        `
        SELECT request_identity
        FROM waje_runtime.run_dispatches
        WHERE run_id = $1
        ORDER BY created_at
        `,
        [runId],
      ),
      loadPersistedPublication(runId),
      pool().query(
        `
        SELECT CASE MAX(
          CASE call_kind
            WHEN 'narrative_provider' THEN 4
            WHEN 'semantic_provider' THEN 3
            WHEN 'query' THEN 2
            WHEN 'capability' THEN 2
            WHEN 'planner_provider' THEN 1
            WHEN 'plan_patch_provider' THEN 1
            ELSE 0
          END
        )
          WHEN 4 THEN 'delivering'
          WHEN 3 THEN 'synthesizing'
          WHEN 2 THEN 'querying'
          WHEN 1 THEN 'planning'
          ELSE 'understanding'
        END AS customer_phase
        FROM waje_runtime.durable_call_attempts
        WHERE run_attempt_id = $1
        `,
        [runId],
      ),
    ]);
    const request = isGatewayRecord(row.request) ? row.request : {};
    const version = versionResult.rows[0];
    return projectCustomerAnalysisSnapshot({
      thread: { id: thread.id, createdAt: thread.createdAt },
      messages,
      run: {
        id: runId,
        status: String(row.status),
        request,
        createdAt: new Date(row.created_at).toISOString(),
        updatedAt: new Date(row.updated_at).toISOString(),
      },
      runNodes: nodesResult.rows.map((node) => ({
        nodeName: String(node.node_name),
        status: String(node.status),
        confirmedAt: node.confirmed_at
          ? new Date(node.confirmed_at).toISOString()
          : null,
      })),
      currentClarification: clarificationResult.rows[0]?.payload ?? null,
      interactionResult: projectInteractionResultForCustomer(
        request.interaction_result,
      ),
      customerPublication: publication?.customerPublication ?? null,
      acceptedOperationIds: dispatchResult.rows.map((dispatch) =>
        String(dispatch.request_identity)
      ),
      progressPhase: progressResult.rows[0]?.customer_phase as CustomerPhase,
      confirmedAt: new Date(version.confirmed_at).toISOString(),
      stateVersion: String(version.state_version),
    });
  }

  const store = memoryStore();
  const run = input.runId
    ? store.runs.get(input.runId) ?? null
    : [...store.runs.values()]
      .filter((candidate) => candidate.threadId === thread.id)
      .sort((left, right) => right.createdAt.localeCompare(left.createdAt))[0] ?? null;
  const messages = thread.messages
    .filter((message) => message.role !== "system")
    .map((message) => ({
      key: message.id,
      role: message.role as "user" | "assistant",
      text: message.text,
      createdAt: message.createdAt,
    }));
  const currentClarification = run
    ? [...store.auditEvents].reverse().find((event) =>
        event.runId === run.id
        && ["clarification_state_saved", "clarification_requested"].includes(
          event.eventType,
        )
      )?.payload ?? null
    : null;
  const confirmedAt = run?.createdAt ?? messages.at(-1)?.createdAt ?? thread.createdAt;
  return projectCustomerAnalysisSnapshot({
    thread: { id: thread.id, createdAt: thread.createdAt },
    messages,
    run: run
      ? {
          id: run.id,
          status: run.status,
          request: run.request ?? {},
          createdAt: run.createdAt,
          updatedAt: run.createdAt,
        }
      : null,
    runNodes: [],
    currentClarification,
    interactionResult: projectInteractionResultForCustomer(
      run?.request?.interaction_result,
    ),
    customerPublication: null,
    acceptedOperationIds: run
      ? [...store.runDispatches.values()]
        .filter((dispatch) => dispatch.runId === run.id)
        .map((dispatch) => dispatch.requestIdentity)
      : [],
    confirmedAt,
    stateVersion: String(Date.parse(confirmedAt)),
  });
}

async function customerStateVersion(threadId: string) {
  return pool().query(
    `
    SELECT
      latest.confirmed_at,
      floor(extract(epoch FROM latest.confirmed_at) * 1000000)::bigint::text
        AS state_version
    FROM (
      SELECT GREATEST(
        thread.updated_at,
        COALESCE((
          SELECT max(run.updated_at)
          FROM waje_runtime.analysis_runs run
          WHERE run.thread_id = thread.thread_id
        ), thread.updated_at),
        COALESCE((
          SELECT max(message.created_at)
          FROM waje_runtime.conversation_messages message
          WHERE message.thread_id = thread.thread_id
        ), thread.updated_at),
        COALESCE((
          SELECT max(event.created_at)
          FROM waje_runtime.audit_events event
          WHERE event.thread_id = thread.thread_id
        ), thread.updated_at)
      ) AS confirmed_at
      FROM waje_runtime.investigation_threads thread
      WHERE thread.thread_id = $1
    ) latest
    `,
    [threadId],
  );
}

function customerThreadTitle(value: string) {
  const title = value.trim();
  if (!title) return "新分析";
  return title.length > 34 ? `${title.slice(0, 34)}…` : title;
}

function customerSummaryStatus(
  runStatus: unknown,
  request: Record<string, unknown>,
  limitationCount: number,
): CustomerMainStatus {
  if (!runStatus) return "idle";
  const status = String(runStatus);
  if (status === "waiting_for_clarification") return "needs_input";
  if (status === "interaction_completed") {
    const interaction = projectInteractionResultForCustomer(
      request.interaction_result,
    );
    return interaction?.schema_version === "typed-topic-choice.v1"
      ? "needs_input"
      : "completed";
  }
  if (status === "failed") return "failed";
  if (status === "completed") {
    if (
      request.post_execution_status !== "completed"
      || request.publication_status !== "published"
      || request.delivery_status !== "published"
    ) return "failed";
    return limitationCount > 0 || request.analysis_status === "boundary_only"
      ? "completed_with_limits"
      : "completed";
  }
  if ([
    "queued",
    "running",
    "running_workflow",
    "planned",
    "evidence_ready",
    "authority_sealed",
    "narrative_ready",
  ].includes(status)) return "working";
  throw gatewayError("customer_run_status_unmapped");
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

export async function claimInitialThreadRequest(
  ownerId: string,
  requestIdentity: string,
): Promise<ThreadRecord> {
  ownerId = normalizeActorId(ownerId);
  requestIdentity = normalizeInitialRequestIdentity(requestIdentity);
  const threadId = `thread-${createHash("sha256")
    .update(JSON.stringify(["customer-initial-thread.v1", ownerId, requestIdentity]))
    .digest("hex")
    .slice(0, 32)}`;

  if (conversationStoreMode() === "postgres") {
    const client = await pool().connect();
    try {
      await client.query("BEGIN");
      const inserted = await client.query(
        `
        INSERT INTO waje_runtime.investigation_threads(thread_id, owner_id)
        VALUES ($1, $2)
        ON CONFLICT (thread_id) DO NOTHING
        RETURNING thread_id, owner_id, created_at
        `,
        [threadId, ownerId],
      );
      const row = inserted.rows[0] ?? (await client.query(
        `
        SELECT thread_id, owner_id, created_at
        FROM waje_runtime.investigation_threads
        WHERE thread_id = $1
        `,
        [threadId],
      )).rows[0];
      if (!row) throw gatewayError("thread_not_found");
      if (row.owner_id !== ownerId) throw gatewayError("thread_owner_mismatch");
      if (inserted.rows[0]) {
        await client.query(
          `
          INSERT INTO waje_runtime.audit_events(
            event_type, actor_id, thread_id, ref, payload
          ) VALUES ('thread_created', $1, $2, $2, $3::jsonb)
          `,
          [ownerId, threadId, JSON.stringify({ source: "customer_initial_request" })],
        );
      }
      await client.query("COMMIT");
      return {
        id: String(row.thread_id),
        ownerId: String(row.owner_id),
        topicIds: [],
        messages: [],
        createdAt: String(row.created_at),
      };
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  }

  const store = memoryStore();
  const existing = store.threads.get(threadId);
  if (existing) {
    if (existing.ownerId !== ownerId) throw gatewayError("thread_owner_mismatch");
    return existing;
  }
  const thread: ThreadRecord = {
    id: threadId,
    ownerId,
    topicIds: [],
    messages: [],
    createdAt: new Date().toISOString(),
  };
  store.threads.set(thread.id, thread);
  store.auditEvents.push({
    eventType: "thread_created",
    actorId: ownerId,
    threadId: thread.id,
    ref: thread.id,
    payload: { source: "customer_initial_request" },
  });
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
      INSERT INTO waje_runtime.analysis_runs(
        run_id, run_attempt_id, thread_id, status
      )
      VALUES ($1, $1, $2, $3)
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

export async function claimRunDispatchRequest(
  input: RunDispatchInput,
): Promise<RunDispatchClaim> {
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
          || (
            normalized.producerKind === "clarification_resolution"
            && existing.run_id !== normalized.runId
          )
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
      const dispatchId = `dispatch-${crypto.randomUUID()}`;
      let run: RunRecord;
      if (normalized.producerKind === "clarification_resolution") {
        const runResult = await client.query(
          `SELECT run_id, thread_id, status, request, created_at
           FROM waje_runtime.analysis_runs
           WHERE run_id = $1 FOR UPDATE`,
          [normalized.runId],
        );
        if (!runResult.rows[0]) throw gatewayError("run_not_found");
        run = runRecordFromRow(runResult.rows[0]);
        if (run.threadId !== normalized.threadId) {
          throw gatewayError("run_dispatch_conflict");
        }
        if (run.status !== "waiting_for_clarification") {
          throw gatewayError("clarification_source_not_waiting");
        }
        const activeResult = await client.query(
          `SELECT dispatch_id
           FROM waje_runtime.run_dispatches
           WHERE run_id = $1
             AND dispatch_state IN ('pending', 'leased', 'running')
           LIMIT 1`,
          [run.id],
        );
        if (activeResult.rows[0]) {
          throw gatewayError("run_dispatch_active_conflict");
        }
      } else {
        const runId = `run-${crypto.randomUUID()}`;
        await client.query(
          `INSERT INTO waje_runtime.analysis_runs(
             run_id, run_attempt_id, thread_id, status
           ) VALUES ($1, $1, $2, 'queued')`,
          [runId, normalized.threadId],
        );
        run = {
          id: runId,
          threadId: normalized.threadId,
          status: "queued",
          createdAt,
          request: {},
        };
      }
      await client.query(
        `INSERT INTO waje_runtime.conversation_messages(message_id, thread_id, role, text)
         VALUES ($1, $2, 'user', $3)`,
        [messageId, normalized.threadId, normalized.text],
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
          run.id,
          messageId,
        ],
      );
      await client.query(
        `
        INSERT INTO waje_runtime.audit_events(event_type, actor_id, thread_id, run_id, ref, payload)
        VALUES
          ('message_recorded', $1, $2, $3, $4, $6::jsonb),
          ($5, $1, $2, $3, $7, $6::jsonb)
        `,
        [
          normalized.actorId,
          normalized.threadId,
          run.id,
          messageId,
          normalized.producerKind === "thread_message"
            ? "run_queued"
            : "run_dispatch_queued",
          JSON.stringify({ dispatchId, producerKind: normalized.producerKind }),
          normalized.producerKind === "thread_message" ? run.id : dispatchId,
        ],
      );
      await client.query("COMMIT");
      return {
        message: { id: messageId, role: "user", text: normalized.text, createdAt },
        run,
        dispatch: {
          dispatchId,
          producerKind: normalized.producerKind,
          scopeRef: normalized.scopeRef,
          requestIdentity: normalized.requestIdentity,
          requestDigest,
          requestPayload: normalized.requestPayload,
          threadId: normalized.threadId,
          runId: run.id,
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
      || (
        normalized.producerKind === "clarification_resolution"
        && existing.runId !== normalized.runId
      )
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
  let run: RunRecord;
  if (normalized.producerKind === "clarification_resolution") {
    const existingRun = store.runs.get(normalized.runId);
    if (!existingRun) throw gatewayError("run_not_found");
    if (existingRun.threadId !== normalized.threadId) {
      throw gatewayError("run_dispatch_conflict");
    }
    if (existingRun.status !== "waiting_for_clarification") {
      throw gatewayError("clarification_source_not_waiting");
    }
    if (
      [...store.runDispatches.values()].some(
        (dispatch) => dispatch.runId === existingRun.id
          && ["pending", "leased", "running"].includes(dispatch.state),
      )
    ) {
      throw gatewayError("run_dispatch_active_conflict");
    }
    run = existingRun;
  } else {
    run = {
      id: `run-${crypto.randomUUID()}`,
      threadId: normalized.threadId,
      status: "queued",
      createdAt,
      request: {},
    };
  }
  const message: MessageRecord = {
    id: `message-${crypto.randomUUID()}`,
    role: "user",
    text: normalized.text,
    createdAt,
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
      payload: {
        dispatchId: dispatch.dispatchId,
        producerKind: normalized.producerKind,
      },
    },
    {
      eventType: normalized.producerKind === "thread_message"
        ? "run_queued"
        : "run_dispatch_queued",
      actorId: normalized.actorId,
      threadId: normalized.threadId,
      runId: run.id,
      ref: normalized.producerKind === "thread_message"
        ? run.id
        : dispatch.dispatchId,
      payload: { dispatchId: dispatch.dispatchId, producerKind: normalized.producerKind },
    },
  ];
  thread.messages.push(message);
  if (normalized.producerKind === "thread_message") {
    store.runs.set(run.id, run);
  }
  store.runDispatches.set(dispatch.dispatchId, dispatch);
  store.auditEvents = stagedAudits;
  return { message, run, dispatch, replayed: false };
}

export async function acquireRunDispatchLease(input: {
  dispatchId: string;
  runId: string;
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
         WHERE dispatch_id = $1 FOR UPDATE`,
        [input.dispatchId],
      );
      const row = dispatchResult.rows[0];
      if (!row) throw gatewayError("run_dispatch_not_found");
      if (row.run_id !== input.runId) {
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
        return { dispatchId: input.dispatchId, acquired: false, ownerId: null, leaseEpoch: Number(row.lease_epoch), state, reason: "terminal", run };
      }
      if (state === "running") {
        await client.query("COMMIT");
        return { dispatchId: input.dispatchId, acquired: false, ownerId: null, leaseEpoch: Number(row.lease_epoch), state, reason: "already_running", run };
      }
      if (!runDispatchCanStart(String(row.producer_kind), run.status)) {
        await client.query("COMMIT");
        return { dispatchId: input.dispatchId, acquired: false, ownerId: null, leaseEpoch: Number(row.lease_epoch), state, reason: "run_not_dispatchable", run };
      }
      if (state === "leased" && row.lease_active === true) {
        await client.query("COMMIT");
        return { dispatchId: input.dispatchId, acquired: false, ownerId: null, leaseEpoch: Number(row.lease_epoch), state, reason: "active_lease", run };
      }
      const updated = await client.query(
        `
        UPDATE waje_runtime.run_dispatches
        SET dispatch_state = 'leased', owner_id = $2,
            lease_epoch = lease_epoch + 1,
            lease_expires_at = now() + ($3 * interval '1 millisecond'),
            heartbeat_at = now(), updated_at = now()
        WHERE dispatch_id = $1 AND run_id = $4
        RETURNING lease_epoch
        `,
        [input.dispatchId, ownerId, leaseMs, input.runId],
      );
      if (!updated.rows[0]) throw gatewayError("run_dispatch_conflict");
      await client.query("COMMIT");
      return { dispatchId: input.dispatchId, acquired: true, ownerId, leaseEpoch: Number(updated.rows[0].lease_epoch), state: "leased", reason: "acquired", run };
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  }
  const store = memoryStore();
  const dispatch = store.runDispatches.get(input.dispatchId);
  if (!dispatch) throw gatewayError("run_dispatch_not_found");
  if (dispatch.runId !== input.runId) {
    throw gatewayError("run_dispatch_conflict");
  }
  const run = store.runs.get(input.runId);
  if (!run) throw gatewayError("run_not_found");
  if (dispatch.state === "terminal") {
    return { dispatchId: input.dispatchId, acquired: false, ownerId: null, leaseEpoch: dispatch.leaseEpoch, state: dispatch.state, reason: "terminal", run };
  }
  if (dispatch.state === "running") {
    return { dispatchId: input.dispatchId, acquired: false, ownerId: null, leaseEpoch: dispatch.leaseEpoch, state: dispatch.state, reason: "already_running", run };
  }
  if (!runDispatchCanStart(dispatch.producerKind, run.status)) {
    return { dispatchId: input.dispatchId, acquired: false, ownerId: null, leaseEpoch: dispatch.leaseEpoch, state: dispatch.state, reason: "run_not_dispatchable", run };
  }
  const expiry = dispatch.leaseExpiresAt ? Date.parse(dispatch.leaseExpiresAt) : 0;
  if (dispatch.state === "leased" && Number.isFinite(expiry) && expiry > Date.now()) {
    return { dispatchId: input.dispatchId, acquired: false, ownerId: null, leaseEpoch: dispatch.leaseEpoch, state: dispatch.state, reason: "active_lease", run };
  }
  dispatch.state = "leased";
  dispatch.ownerId = ownerId;
  dispatch.leaseEpoch += 1;
  dispatch.leaseExpiresAt = new Date(Date.now() + leaseMs).toISOString();
  dispatch.heartbeatAt = new Date().toISOString();
  return { dispatchId: input.dispatchId, acquired: true, ownerId, leaseEpoch: dispatch.leaseEpoch, state: "leased", reason: "acquired", run };
}

export async function failOwnedRunDispatch(input: {
  dispatchId: string;
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
         WHERE dispatch_id = $1 FOR UPDATE`,
        [input.dispatchId],
      );
      const dispatch = dispatchResult.rows[0];
      if (!dispatch) throw gatewayError("run_dispatch_not_found");
      if (dispatch.run_id !== input.runId) {
        throw gatewayError("run_dispatch_conflict");
      }
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
        || ![
          "queued",
          "running",
          "running_workflow",
          "waiting_for_clarification",
        ].includes(current.status)
      ) {
        await client.query("COMMIT");
        return current;
      }
      const preserveWaitingRun = dispatch.producer_kind === "clarification_resolution"
        && current.status === "waiting_for_clarification";
      let durableRun = current;
      if (!preserveWaitingRun) {
        const failedResult = await client.query(
          `UPDATE waje_runtime.analysis_runs
           SET status = 'failed',
               request = COALESCE(request, '{}'::jsonb)
                 || jsonb_build_object('failure_reason', $2),
               updated_at = now()
           WHERE run_id = $1
             AND status IN ('queued', 'running', 'running_workflow')
           RETURNING run_id, thread_id, status, request, created_at`,
          [input.runId, input.failureReason],
        );
        if (!failedResult.rows[0]) throw gatewayError("run_dispatch_lease_lost");
        durableRun = runRecordFromRow(failedResult.rows[0]);
      }
      await client.query(
        `UPDATE waje_runtime.run_dispatches
         SET dispatch_state = 'terminal', terminal_status = 'failed',
             failure_reason = $4, lease_expires_at = NULL, updated_at = now()
         WHERE dispatch_id = $1 AND run_id = $5
           AND owner_id = $2 AND lease_epoch = $3`,
        [input.dispatchId, input.ownerId, input.leaseEpoch, input.failureReason, input.runId],
      );
      if (!preserveWaitingRun) {
        await client.query(
          `INSERT INTO waje_runtime.audit_events(
             event_type, actor_id, thread_id, run_id, ref, payload
           ) VALUES (
             'run_status_changed', 'system', $1, $2, $2, $3::jsonb
           )`,
          [current.threadId, input.runId, JSON.stringify({ status: "failed" })],
        );
      }
      await client.query(
        `INSERT INTO waje_runtime.audit_events(
           event_type, actor_id, thread_id, run_id, ref, payload
         ) VALUES (
           'run_dispatch_failed', 'system', $1, $2, $3, $4::jsonb
         )`,
        [
          current.threadId,
          input.runId,
          input.dispatchId,
          JSON.stringify({ dispatchId: input.dispatchId, failureReason: input.failureReason, leaseEpoch: input.leaseEpoch }),
        ],
      );
      await client.query("COMMIT");
      return durableRun;
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  }
  const store = memoryStore();
  const dispatch = store.runDispatches.get(input.dispatchId);
  if (!dispatch) throw gatewayError("run_dispatch_not_found");
  if (dispatch.runId !== input.runId) throw gatewayError("run_dispatch_conflict");
  const current = store.runs.get(input.runId);
  if (!current) throw gatewayError("run_not_found");
  if (
    !["leased", "running"].includes(dispatch.state)
    || dispatch.ownerId !== input.ownerId
    || dispatch.leaseEpoch !== input.leaseEpoch
    || ![
      "queued",
      "running",
      "running_workflow",
      "waiting_for_clarification",
    ].includes(current.status)
  ) {
    return current;
  }
  const preserveWaitingRun = dispatch.producerKind === "clarification_resolution"
    && current.status === "waiting_for_clarification";
  const failed: RunRecord = preserveWaitingRun
    ? current
    : {
        ...current,
        status: "failed",
        request: { ...(current.request ?? {}), failure_reason: input.failureReason },
      };
  dispatch.state = "terminal";
  dispatch.terminalStatus = "failed";
  dispatch.failureReason = input.failureReason;
  dispatch.leaseExpiresAt = null;
  if (!preserveWaitingRun) store.runs.set(input.runId, failed);
  store.auditEvents = [
    ...store.auditEvents,
    ...(!preserveWaitingRun
      ? [{ eventType: "run_status_changed", threadId: current.threadId, runId: current.id, ref: current.id, payload: { status: "failed" } }]
      : []),
    { eventType: "run_dispatch_failed", threadId: current.threadId, runId: current.id, ref: input.dispatchId, payload: { dispatchId: input.dispatchId, failureReason: input.failureReason, leaseEpoch: input.leaseEpoch } },
  ];
  return failed;
}

export async function observeOwnedRunDispatchExit(input: {
  dispatchId: string;
  runId: string;
  ownerId: string;
  leaseEpoch: number;
  failureReason?: string;
}): Promise<RunRecord> {
  if (conversationStoreMode() === "postgres") {
    const client = await pool().connect();
    try {
      await client.query("BEGIN");
      const result = await client.query(
        `SELECT dispatch.producer_kind, dispatch.dispatch_state,
                dispatch.owner_id, dispatch.lease_epoch,
                run.run_id, run.thread_id, run.status, run.request,
                run.created_at
         FROM waje_runtime.run_dispatches dispatch
         JOIN waje_runtime.analysis_runs run ON run.run_id = dispatch.run_id
         WHERE dispatch.dispatch_id = $1 AND dispatch.run_id = $2
         FOR UPDATE OF dispatch, run`,
        [input.dispatchId, input.runId],
      );
      const row = result.rows[0];
      if (!row) throw gatewayError("run_dispatch_not_found");
      const current = runRecordFromRow(row);
      if (
        row.producer_kind === "clarification_resolution"
        && current.status === "waiting_for_clarification"
        && ["leased", "running"].includes(String(row.dispatch_state))
        && row.owner_id === input.ownerId
        && Number(row.lease_epoch) === input.leaseEpoch
      ) {
        const released = await client.query(
          `UPDATE waje_runtime.run_dispatches
           SET dispatch_state = 'pending', owner_id = NULL,
               lease_expires_at = NULL, heartbeat_at = NULL,
               terminal_status = NULL, failure_reason = NULL,
               updated_at = now()
           WHERE dispatch_id = $1 AND run_id = $2
             AND owner_id = $3 AND lease_epoch = $4
             AND dispatch_state IN ('leased', 'running')
           RETURNING dispatch_id`,
          [input.dispatchId, input.runId, input.ownerId, input.leaseEpoch],
        );
        if (!released.rows[0]) throw gatewayError("run_dispatch_lease_lost");
        await client.query(
          `INSERT INTO waje_runtime.audit_events(
             event_type, actor_id, thread_id, run_id, ref, payload
           ) VALUES (
             'run_dispatch_recovery_requested', 'system', $1, $2, $3, $4::jsonb
           )`,
          [
            current.threadId,
            input.runId,
            input.dispatchId,
            JSON.stringify({
              dispatchId: input.dispatchId,
              failureReason: input.failureReason ?? "agent_core_worker_exited",
              leaseEpoch: input.leaseEpoch,
            }),
          ],
        );
        await client.query("COMMIT");
        return current;
      }
      await client.query("COMMIT");
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  } else {
    const store = memoryStore();
    const dispatch = store.runDispatches.get(input.dispatchId);
    const current = store.runs.get(input.runId);
    if (!dispatch || !current || dispatch.runId !== input.runId) {
      throw gatewayError("run_dispatch_not_found");
    }
    if (
      dispatch.producerKind === "clarification_resolution"
      && current.status === "waiting_for_clarification"
      && ["leased", "running"].includes(dispatch.state)
      && dispatch.ownerId === input.ownerId
      && dispatch.leaseEpoch === input.leaseEpoch
    ) {
      dispatch.state = "pending";
      dispatch.ownerId = null;
      dispatch.leaseExpiresAt = null;
      dispatch.heartbeatAt = null;
      dispatch.terminalStatus = null;
      dispatch.failureReason = null;
      store.auditEvents = [
        ...store.auditEvents,
        {
          eventType: "run_dispatch_recovery_requested",
          threadId: current.threadId,
          runId: current.id,
          ref: input.dispatchId,
          payload: {
            dispatchId: input.dispatchId,
            failureReason: input.failureReason ?? "agent_core_worker_exited",
            leaseEpoch: input.leaseEpoch,
          },
        },
      ];
      return current;
    }
  }
  return failOwnedRunDispatch({
    ...input,
    failureReason: input.failureReason ?? "agent_core_worker_exited",
  });
}

export async function completeOwnedRunDispatch(input: {
  dispatchId: string;
  runId: string;
  ownerId: string;
  leaseEpoch: number;
  runStatus: "interaction_completed" | "waiting_for_clarification" | "planned" | "evidence_ready" | "authority_sealed" | "narrative_ready" | "completed" | "failed";
}): Promise<RunRecord> {
  if (conversationStoreMode() === "postgres") {
    const client = await pool().connect();
    try {
      await client.query("BEGIN");
      const dispatchResult = await client.query(
        `SELECT * FROM waje_runtime.run_dispatches
         WHERE dispatch_id = $1 FOR UPDATE`,
        [input.dispatchId],
      );
      const dispatch = dispatchResult.rows[0];
      if (!dispatch) throw gatewayError("run_dispatch_not_found");
      if (dispatch.run_id !== input.runId) {
        throw gatewayError("run_dispatch_conflict");
      }
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
      const runCanFinish = [
        "queued",
        "running",
        "running_workflow",
        "waiting_for_clarification",
      ].includes(current.status);
      if (runCanFinish) {
        const updated = await client.query(
          `UPDATE waje_runtime.analysis_runs
           SET status = $2, updated_at = now()
           WHERE run_id = $1
             AND status IN (
               'queued', 'running', 'running_workflow',
               'waiting_for_clarification'
             )
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
         WHERE dispatch_id = $1 AND run_id = $5
           AND owner_id = $2 AND lease_epoch = $3
           AND dispatch_state IN ('leased', 'running')`,
        [input.dispatchId, input.ownerId, input.leaseEpoch, current.status, input.runId],
      );
      await client.query(
        `INSERT INTO waje_runtime.audit_events(event_type, actor_id, thread_id, run_id, ref, payload)
         VALUES ('run_dispatch_completed', 'system', $1, $2, $4, $3::jsonb)`,
        [
          current.threadId,
          input.runId,
          JSON.stringify({ dispatchId: input.dispatchId, status: current.status, leaseEpoch: input.leaseEpoch }),
          input.dispatchId,
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
  const dispatch = store.runDispatches.get(input.dispatchId);
  if (!dispatch) throw gatewayError("run_dispatch_not_found");
  if (dispatch.runId !== input.runId) throw gatewayError("run_dispatch_conflict");
  const current = store.runs.get(input.runId);
  if (!current) throw gatewayError("run_not_found");
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
  const completed = [
    "queued",
    "running",
    "running_workflow",
    "waiting_for_clarification",
  ].includes(current.status)
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
      ref: input.dispatchId,
      payload: { dispatchId: input.dispatchId, status: completed.status, leaseEpoch: input.leaseEpoch },
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


export async function requireRun(runId: string, actorId: string): Promise<RunRecord> {
  actorId = normalizeActorId(actorId);
  if (conversationStoreMode() === "postgres") {
    const { rows } = await pool().query(
      `
      SELECT r.run_id, r.thread_id, r.status, r.request, r.created_at, t.owner_id
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

export function recordCustomerRunStateFromAgentResult(
  runId: string,
  value: Record<string, unknown> | null,
) {
  if (!value || conversationStoreMode() !== "memory") return;
  const run = memoryStore().runs.get(runId);
  if (!run) throw gatewayError("run_not_found");
  const interaction = projectInteractionResultForCustomer(value.interaction_result);
  const clarification = filterBusinessClarification(value.clarification);
  const postExecution = projectPostExecutionStateForCustomer(value);
  run.request = {
    ...(run.request ?? {}),
    ...(interaction ? { interaction_result: interaction } : {}),
    ...(postExecution ? postExecution : {}),
  };
  if (clarification) {
    memoryStore().auditEvents.push({
      eventType: "clarification_state_saved",
      threadId: run.threadId,
      runId,
      payload: { ...clarification, status: "waiting" },
    });
  }
}

export async function runEvents(runId: string, actorId: string): Promise<RunEvent[]> {
  const run = await requireRun(runId, actorId);
  if (conversationStoreMode() === "postgres") {
    const postExecution = projectPostExecutionStateForCustomer(run.request);
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
      const visibleAuditPayload = projectCustomerAuditPayload(
        row.event_type,
        row.payload,
      );
      events.push({
        event: row.event_type,
        runId,
        threadId: run.threadId,
        payload: visibleAuditPayload,
        process: processEvent(row.event_type, visibleAuditPayload),
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
      const payload = projectCustomerNodePayload({
        node_name: row.node_name,
        status: row.status,
        started_at: row.started_at,
        finished_at: row.finished_at,
      });
      events.push({
        event: "node_process",
        runId,
        threadId: run.threadId,
        payload,
        process: processNodeEvent(row.node_name, row.status, payload),
      });
    }
    if (run.status === "interaction_completed") {
      const interaction = projectInteractionResultForCustomer(
        run.request?.interaction_result,
      );
      if (!interaction) {
        throw gatewayError("interaction_result_missing");
      }
      const continuation = interaction.intent === "material_revision"
        ? projectMaterialRevisionContinuationForCustomer(
            run.request?.material_revision_continuation,
            runId,
          )
        : null;
      if (interaction.intent === "material_revision" && !continuation) {
        throw gatewayError("material_revision_continuation_invalid");
      }
      events.push({
        event: "interaction_result_ready",
        runId,
        threadId: run.threadId,
        payload: {
          status: "interaction_completed",
          interaction_result: interaction,
          ...(continuation ? { continuation } : {}),
        },
        process: processEvent("interaction_result_ready", {
          status: "interaction_completed",
        }),
      });
    }
    const requestHasPlanResultRefs = Object.prototype.hasOwnProperty.call(
      run.request ?? {},
      "plan_result_refs",
    );
    const requestHasExecutionResultRefs = Object.prototype.hasOwnProperty.call(
      run.request ?? {},
      "execution_result_refs",
    );
    const requestHasClaimCoverageRefs = Object.prototype.hasOwnProperty.call(
      run.request ?? {},
      "claim_coverage_refs",
    );
    if (
      PLAN_RESULT_EVENT_STATUSES.has(run.status)
      || (run.status === "failed" && requestHasPlanResultRefs)
    ) {
      const planResult = await loadProjectedPlannedResult(runId);
      events.push({
        event: "plan_result_ready",
        runId,
        threadId: run.threadId,
        payload: {
          status: "planned",
          terminal: run.status === "planned",
          plan_result: planResult,
        },
        process: processEvent("plan_result_ready", { status: "planned" }),
      });
    }
    if (
      EXECUTION_RESULT_EVENT_STATUSES.has(run.status)
      || (run.status === "failed" && requestHasExecutionResultRefs)
    ) {
      const executionResult = await loadProjectedExecutionResult(runId);
      events.push({
        event: "execution_result_ready",
        runId,
        threadId: run.threadId,
        payload: {
          status: "evidence_ready",
          terminal: false,
          execution_result: executionResult,
        },
        process: processEvent(
          "execution_result_ready",
          { status: "evidence_ready" },
        ),
      });
    }
    if (requestHasClaimCoverageRefs) {
      const claimCoverage = projectClaimCoverageRefsForCustomer(
        run.request?.claim_coverage_refs,
      );
      if (!claimCoverage) {
        throw gatewayError("claim_coverage_authority_refs_invalid");
      }
      events.push({
        event: "claim_coverage_ready",
        runId,
        threadId: run.threadId,
        payload: {
          ...claimCoverage,
          terminal: run.status === "evidence_ready"
            && claimCoverage.decision === "seal",
        },
        process: processEvent("claim_coverage_ready", claimCoverage),
      });
    }
    if (
      run.status === "authority_sealed"
      || run.status === "narrative_ready"
    ) {
      if (!postExecution) {
        throw gatewayError("post_execution_state_missing");
      }
      events.push({
        event: "post_execution_state",
        runId,
        threadId: run.threadId,
        payload: postExecution,
        process: processEvent("post_execution_state", postExecution),
      });
    }
    if (run.status === "completed") {
      const completed = await loadPersistedPublication(runId);
      if (completed) {
        if (!postExecution) {
          throw gatewayError("post_execution_state_missing");
        }
        events.push({
          event: "customer_publication_ready",
          runId,
          threadId: run.threadId,
          payload: {
            status: "completed",
            customer_publication: completed.customerPublication,
            publication: completed.publication,
            post_execution: postExecution,
          },
          process: processEvent("customer_publication_ready", {
            status: "completed",
            delivery_status: completed.publication.delivery_status,
          }),
        });
      } else if (
        postExecution
        && postExecution.post_execution_status !== "completed"
      ) {
        events.push({
          event: "post_execution_state",
          runId,
          threadId: run.threadId,
          payload: postExecution,
          process: processEvent("post_execution_state", postExecution),
        });
      } else {
        throw gatewayError("completed_publication_missing");
      }
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

async function loadProjectedPlannedResult(runId: string) {
  const { rows } = await pool().query(
    `
    SELECT
      r.run_id,
      r.run_attempt_id,
      r.request -> 'plan_result_refs' AS plan_result_refs,
      context.payload AS authority_context,
      proposal.payload AS planner_proposal,
      admission.payload AS proposal_admission_record,
      plan.payload AS plan_revision,
      supersession.superseded_plan_revision_id,
      accepted.transition_id AS accepted_transition_id,
      accepted.node_name AS accepted_node_name,
      accepted.decision_ledger_position
    FROM waje_runtime.analysis_runs r
    LEFT JOIN waje_runtime.plan_revisions plan
      ON plan.plan_revision_id = r.request #>> '{plan_result_refs,plan_revision_id}'
     AND plan.run_attempt_id = r.run_id
    LEFT JOIN waje_runtime.plan_revision_supersessions supersession
      ON supersession.superseded_plan_revision_id = plan.plan_revision_id
    LEFT JOIN waje_runtime.authority_contexts context
      ON context.authority_context_ref = r.request #>> '{plan_result_refs,authority_context_ref}'
     AND context.run_attempt_id = r.run_id
    LEFT JOIN waje_runtime.planner_proposals proposal
      ON proposal.planner_proposal_id = r.request #>> '{plan_result_refs,planner_proposal_id}'
     AND proposal.run_attempt_id = r.run_id
    LEFT JOIN waje_runtime.proposal_admission_records admission
      ON admission.proposal_admission_id = r.request #>> '{plan_result_refs,proposal_admission_id}'
     AND admission.planner_proposal_ref = proposal.planner_proposal_id
    LEFT JOIN waje_runtime.workflow_transition_attempts accepted
      ON accepted.transition_id = r.request #>> '{plan_result_refs,accepted_transition_id}'
     AND accepted.run_attempt_id = r.run_id
     AND accepted.node_name IN ('compile_authoritative_plan', 'compile_plan_patch')
     AND accepted.status = 'succeeded'
     AND accepted.acceptance_state = 'accepted'
    WHERE r.run_id = $1
      AND r.status IN (
        'planned',
        'evidence_ready',
        'authority_sealed',
        'narrative_ready',
        'completed',
        'failed'
      )
    `,
    [runId],
  );
  const row = rows[0];
  if (!row || !isGatewayRecord(row.plan_result_refs)) {
    throw gatewayError("planned_result_authority_refs_invalid");
  }
  const refs = row.plan_result_refs;
  const planRefFields = new Set([
    "schema_version",
    "plan_patch_ref",
    "intent_revision_id",
    "authority_context_ref",
    "planner_proposal_id",
    "proposal_admission_id",
    "plan_revision_id",
    "accepted_transition_id",
  ]);
  const authorityRefs = projectAuthorityRefs(refs);
  const schemaVersion = businessString(refs.schema_version);
  if (
    Object.keys(refs).length !== planRefFields.size
    || Object.keys(refs).some((field) => !planRefFields.has(field))
    || !authorityRefs
    || schemaVersion !== "single-authority-phase02.v2"
  ) {
    throw gatewayError("planned_result_authority_refs_invalid");
  }
  const authorityBundle = requirePlanAuthorityBundle(row);
  if (
    row.superseded_plan_revision_id
    || row.accepted_transition_id !== refs.accepted_transition_id
    || !Number.isSafeInteger(Number(row.decision_ledger_position))
  ) {
    throw gatewayError("planned_result_authority_mismatch");
  }
  const plan = authorityBundle.planRevision;
  const isPlanPatch = isNonEmptyGatewayString(plan.supersedes_plan_revision_id);
  const planPatchRef = refs.plan_patch_ref;
  if (
    plan.plan_revision_id !== refs.plan_revision_id
    || plan.intent_revision_id !== refs.intent_revision_id
    || plan.authority_context_ref !== refs.authority_context_ref
    || plan.planner_proposal_ref !== refs.planner_proposal_id
    || plan.proposal_admission_ref !== refs.proposal_admission_id
    || row.accepted_node_name
      !== (isPlanPatch ? "compile_plan_patch" : "compile_authoritative_plan")
    || (
      isPlanPatch
        ? !isContentAddressedPlanPatchRef(planPatchRef)
        : plan.supersedes_plan_revision_id !== null || planPatchRef !== null
    )
  ) {
    throw gatewayError("planned_result_authority_mismatch");
  }
  return projectPlanResultForCustomer({
    schema_version: schemaVersion,
    run_id: row.run_id,
    run_attempt_id: row.run_attempt_id,
    status: "planned",
    intent_revision_id: refs.intent_revision_id,
    plan_patch_ref: planPatchRef,
    decision_ledger_position: Number(row.decision_ledger_position),
    decision_refs: plan.decision_refs,
    authority_context: authorityBundle.authorityContext,
    planner_proposal: authorityBundle.plannerProposal,
    proposal_admission_record: authorityBundle.proposalAdmissionRecord,
    plan_revision: plan,
    authority_refs: authorityRefs,
  });
}

const EXECUTION_RESULT_REF_FIELDS = new Set([
  "schema_version",
  "authoritative_execution_result_ref",
  "intent_revision_id",
  "authority_context_ref",
  "plan_revision_id",
  "execution_snapshot_ref",
  "stop_ref",
  "accepted_transition_id",
]);

async function loadProjectedExecutionResult(runId: string) {
  const { rows } = await pool().query(
    `
    SELECT
      r.run_id,
      r.run_attempt_id,
      r.status AS run_status,
      r.request -> 'execution_result_refs' AS execution_result_refs,
      r.request -> 'claim_coverage_refs' AS claim_coverage_refs,
      plan.payload AS plan_revision,
      supersession.superseded_plan_revision_id,
      snapshot.payload AS execution_snapshot,
      stop.payload AS exploration_stop_record,
      accepted.transition_id AS accepted_transition_id,
      accepted.attempt_id AS transition_attempt_id,
      accepted.node_name AS transition_node_name,
      accepted.parent_transition_id AS transition_parent_transition_id,
      accepted.run_attempt_id AS transition_run_attempt_id,
      accepted.intent_revision_id AS transition_intent_revision_id,
      accepted.decision_ledger_position AS transition_decision_ledger_position,
      accepted.input_digest AS transition_input_digest,
      accepted.output_digest AS transition_output_digest,
      accepted.input_payload AS transition_input_payload,
      accepted.output_payload AS transition_output_payload,
      accepted.execution_attempt AS transition_execution_attempt,
      accepted.status AS transition_status,
      accepted.acceptance_state AS transition_acceptance_state,
      accepted.next_transition AS transition_next_transition,
      (
        SELECT latest.transition_id
        FROM waje_runtime.workflow_transition_attempts latest
        WHERE latest.run_attempt_id = r.run_id
          AND latest.acceptance_state = 'accepted'
        ORDER BY latest.created_at DESC, latest.attempt_id DESC
        LIMIT 1
      ) AS latest_accepted_transition_id
    FROM waje_runtime.analysis_runs r
    LEFT JOIN waje_runtime.plan_revisions plan
      ON plan.plan_revision_id = r.request #>> '{execution_result_refs,plan_revision_id}'
     AND plan.run_attempt_id = r.run_id
    LEFT JOIN waje_runtime.plan_revision_supersessions supersession
      ON supersession.superseded_plan_revision_id = plan.plan_revision_id
    LEFT JOIN waje_runtime.capability_execution_snapshots snapshot
      ON snapshot.execution_snapshot_ref = r.request #>> '{execution_result_refs,execution_snapshot_ref}'
     AND snapshot.run_attempt_id = r.run_id
     AND snapshot.plan_revision_id = plan.plan_revision_id
    LEFT JOIN waje_runtime.exploration_stop_records stop
      ON stop.stop_ref = r.request #>> '{execution_result_refs,stop_ref}'
     AND stop.run_attempt_id = r.run_id
     AND stop.plan_revision_id = plan.plan_revision_id
    LEFT JOIN waje_runtime.workflow_transition_attempts accepted
      ON accepted.transition_id = r.request #>> '{execution_result_refs,accepted_transition_id}'
     AND accepted.run_attempt_id = r.run_id
     AND accepted.node_name = 'execute_capability_dag'
     AND accepted.status = 'succeeded'
     AND accepted.acceptance_state = 'accepted'
    WHERE r.run_id = $1
      AND r.status IN (
        'evidence_ready',
        'authority_sealed',
        'narrative_ready',
        'completed',
        'failed'
      )
    `,
    [runId],
  );
  const row = rows[0];
  if (!row || !isGatewayRecord(row.execution_result_refs)) {
    throw gatewayError("execution_result_authority_refs_invalid");
  }
  const refs = row.execution_result_refs;
  const claimCoverage = projectClaimCoverageRefsForCustomer(
    row.claim_coverage_refs,
  );
  const refKeys = Object.keys(refs);
  if (
    refKeys.length !== EXECUTION_RESULT_REF_FIELDS.size
    || refKeys.some((field) => !EXECUTION_RESULT_REF_FIELDS.has(field))
    || refs.schema_version !== "single-authority-phase03.v1"
    || !isContentAddressedExecutionResultRef(
      refs.authoritative_execution_result_ref,
    )
    || ![
      "intent_revision_id",
      "authority_context_ref",
      "plan_revision_id",
      "execution_snapshot_ref",
      "stop_ref",
      "accepted_transition_id",
    ].every((field) => isNonEmptyGatewayString(refs[field]))
  ) {
    throw gatewayError("execution_result_authority_refs_invalid");
  }
  if (
    row.run_id !== runId
    || row.run_attempt_id !== runId
    || row.superseded_plan_revision_id
    || !isGatewayRecord(row.plan_revision)
    || !isGatewayRecord(row.execution_snapshot)
    || !isGatewayRecord(row.exploration_stop_record)
    || row.accepted_transition_id !== refs.accepted_transition_id
    || !isNonEmptyGatewayString(row.latest_accepted_transition_id)
    || !claimCoverage
    || claimCoverage.source_plan_revision_id !== refs.plan_revision_id
    || claimCoverage.source_execution_result_ref
      !== refs.authoritative_execution_result_ref
    || (
      row.run_status === "evidence_ready"
      && row.latest_accepted_transition_id
        !== claimCoverage.accepted_transition_id
    )
    || !isNonEmptyGatewayString(row.transition_attempt_id)
    || !isNonEmptyGatewayString(row.transition_parent_transition_id)
    || row.transition_run_attempt_id !== runId
    || row.transition_intent_revision_id !== refs.intent_revision_id
    || row.transition_node_name !== "execute_capability_dag"
    || row.transition_status !== "succeeded"
    || row.transition_acceptance_state !== "accepted"
    || row.transition_next_transition !== "phase03_evidence_bound"
    || !isGatewayDigest(row.transition_input_digest)
    || !isGatewayDigest(row.transition_output_digest)
    || !isGatewayRecord(row.transition_input_payload)
    || !isGatewayRecord(row.transition_output_payload)
    || !Number.isSafeInteger(Number(row.transition_decision_ledger_position))
    || Number(row.transition_decision_ledger_position) < 0
    || !Number.isSafeInteger(Number(row.transition_execution_attempt))
    || Number(row.transition_execution_attempt) < 1
  ) {
    throw gatewayError("execution_result_authority_mismatch");
  }

  const plan = row.plan_revision;
  const snapshot = row.execution_snapshot;
  const stop = row.exploration_stop_record;
  if (
    plan.run_attempt_id !== runId
    || plan.intent_revision_id !== refs.intent_revision_id
    || plan.authority_context_ref !== refs.authority_context_ref
    || plan.plan_revision_id !== refs.plan_revision_id
    || snapshot.run_attempt_id !== runId
    || snapshot.authority_context_ref !== refs.authority_context_ref
    || snapshot.plan_revision_id !== refs.plan_revision_id
    || snapshot.execution_snapshot_ref !== refs.execution_snapshot_ref
    || snapshot.stop_ref !== refs.stop_ref
    || stop.run_attempt_id !== runId
    || stop.plan_revision_id !== refs.plan_revision_id
    || stop.stop_ref !== refs.stop_ref
  ) {
    throw gatewayError("execution_result_authority_mismatch");
  }
  const transitionInput = {
    plan_revision_id: plan.plan_revision_id,
    plan_digest: plan.content_digest,
    authority_context_ref: plan.authority_context_ref,
    budget_policy_ref: plan.budget_policy_ref,
    hard_budget_limit: stop.hard_budget_limit,
    capability_tasks: [...plan.capability_tasks]
      .filter(isGatewayRecord)
      .sort((left, right) => (
        String(left.task_id).localeCompare(String(right.task_id))
      ))
      .map((task) => ({
        task_id: task.task_id,
        idempotency_key: task.idempotency_key,
      })),
  };
  const transitionOutput = {
    execution_snapshot: snapshot,
    exploration_stop_record: stop,
  };
  if (
    !isGatewayDigest(plan.content_digest)
    || !isNonEmptyGatewayString(plan.budget_policy_ref)
    || transitionInput.capability_tasks.length !== plan.capability_tasks.length
    || transitionInput.capability_tasks.some((task) => (
      !isNonEmptyGatewayString(task.task_id)
      || !isNonEmptyGatewayString(task.idempotency_key)
    ))
    || gatewayValueDigest(transitionInput) !== row.transition_input_digest
    || gatewayValueDigest(row.transition_input_payload)
      !== row.transition_input_digest
    || gatewayValueDigest(transitionOutput) !== row.transition_output_digest
    || gatewayValueDigest(row.transition_output_payload)
      !== row.transition_output_digest
  ) {
    throw gatewayError("execution_result_transition_payload_mismatch");
  }

  const bundleRows = await pool().query(
    `
    SELECT
      attempt.payload AS attempt_payload,
      outcome.payload AS outcome_payload
    FROM waje_runtime.capability_outcomes outcome
    JOIN waje_runtime.capability_task_attempts attempt
      ON attempt.attempt_id = outcome.attempt_id
    WHERE outcome.run_attempt_id = $1
      AND outcome.plan_revision_id = $2
    ORDER BY outcome.outcome_ref
    `,
    [runId, refs.plan_revision_id],
  );
  const evidenceRows = await pool().query(
    `
    SELECT payload
    FROM waje_runtime.capability_evidence_ledger_entries
    WHERE run_attempt_id = $1 AND plan_revision_id = $2
    ORDER BY entry_ref
    `,
    [runId, refs.plan_revision_id],
  );
  const failureRows = await pool().query(
    `
    SELECT payload
    FROM waje_runtime.capability_failure_records
    WHERE run_attempt_id = $1 AND plan_revision_id = $2
    ORDER BY failure_ref
    `,
    [runId, refs.plan_revision_id],
  );
  const evidenceEntries = evidenceRows.rows.map((item) => item.payload);
  const failureRecords = failureRows.rows.map((item) => item.payload);
  if (
    evidenceEntries.some((item) => !isGatewayRecord(item))
    || failureRecords.some((item) => !isGatewayRecord(item))
  ) {
    throw gatewayError("execution_result_authority_bundle_invalid");
  }
  const bundles = bundleRows.rows.map((item) => ({
    attempt: item.attempt_payload,
    outcome: item.outcome_payload,
    evidence_entries: evidenceEntries.filter(
      (entry) => entry.outcome_ref === item.outcome_payload?.outcome_ref,
    ),
    failure_records: failureRecords.filter(
      (failure) => failure.attempt_id === item.attempt_payload?.attempt_id,
    ),
  }));
  if (
    bundles.reduce(
      (count, bundle) => count + bundle.evidence_entries.length,
      0,
    ) !== evidenceEntries.length
    || bundles.reduce(
      (count, bundle) => count + bundle.failure_records.length,
      0,
    ) !== failureRecords.length
  ) {
    throw gatewayError("execution_result_authority_bundle_invalid");
  }
  const closure = {
    runAttemptId: runId,
    intentRevisionId: String(refs.intent_revision_id),
    authorityContextRef: String(refs.authority_context_ref),
    planRevisionId: String(refs.plan_revision_id),
    executionSnapshotRef: String(refs.execution_snapshot_ref),
    stopRef: String(refs.stop_ref),
    planRevision: plan,
    executionSnapshot: snapshot,
    explorationStopRecord: stop,
    bundles,
  };
  if (!hasExecutionProjectionClosure(closure)) {
    throw gatewayError("execution_result_authority_bundle_invalid");
  }
  return projectExecutionAuthorityForCustomer({
    resultRef: String(refs.authoritative_execution_result_ref),
    planRevisionId: String(refs.plan_revision_id),
    executionSnapshotRef: String(refs.execution_snapshot_ref),
    planRevision: plan,
    explorationStopRecord: stop,
    bundles,
  });
}

function isContentAddressedExecutionResultRef(value: unknown) {
  return typeof value === "string"
    && /^authoritative-execution-result:sha256:[0-9a-f]{64}$/.test(value);
}

function isContentAddressedPlanPatchRef(value: unknown) {
  return typeof value === "string"
    && /^plan-patch:sha256:[0-9a-f]{64}$/.test(value);
}

function isGatewayDigest(value: unknown) {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function projectCustomerAuditPayload(eventType: string, value: unknown) {
  const payload = isGatewayRecord(value) ? value : {};
  if (eventType === "clarification_requested") {
    const clarification = filterBusinessClarification(payload);
    if (!clarification) throw gatewayError("clarification_payload_invalid");
    return clarification;
  }
  if (eventType === "clarification_state_saved") {
    const clarification = filterBusinessClarificationState(payload);
    if (!clarification) throw gatewayError("clarification_state_invalid");
    return clarification;
  }
  if (RUN_DISPATCH_EVENT_STATES.has(eventType)) {
    const dispatchId = businessString(
      payload.dispatch_id ?? payload.dispatchId,
    );
    if (!dispatchId) throw gatewayError("run_dispatch_event_invalid");
    const terminalStatus = eventType === "run_dispatch_completed"
      ? businessString(payload.status)
      : eventType === "run_dispatch_failed"
      ? "failed"
      : undefined;
    if (
      (eventType === "run_dispatch_completed" && !terminalStatus)
      || (terminalStatus && !RUN_STATUSES.includes(terminalStatus as RunStatus))
    ) {
      throw gatewayError("run_dispatch_event_invalid");
    }
    return compactGatewayRecord({
      dispatch_id: dispatchId,
      producer_kind: businessString(
        payload.producer_kind ?? payload.producerKind,
      ),
      state: RUN_DISPATCH_EVENT_STATES.get(eventType),
      terminal_status: terminalStatus,
    });
  }
  return compactGatewayRecord({
    status: businessString(payload.status),
    schema_version: businessString(payload.schema_version),
    intent_revision_id: businessString(payload.intent_revision_id),
    plan_revision_id: businessString(payload.plan_revision_id),
    authority_context_ref: businessString(payload.authority_context_ref),
    authoritative_execution_result_ref: businessString(
      payload.authoritative_execution_result_ref,
    ),
    execution_snapshot_ref: businessString(payload.execution_snapshot_ref),
    stop_ref: businessString(payload.stop_ref),
    accepted_transition_id: businessString(payload.accepted_transition_id),
    outcome_ref: businessString(payload.outcome_ref),
    evidence_ref: businessString(payload.evidence_ref),
    affected_obligation_ids: projectBusinessStringArray(
      payload.affected_obligation_ids,
    ),
    limitation_refs: projectBusinessStringArray(payload.limitation_refs),
  });
}

const RUN_DISPATCH_EVENT_STATES: ReadonlyMap<string, string> = new Map([
  ["run_dispatch_queued", "pending"],
  ["run_dispatch_claimed", "running"],
  ["run_dispatch_recovery_requested", "pending"],
  ["run_dispatch_completed", "terminal"],
  ["run_dispatch_failed", "terminal"],
]);

function projectCustomerNodePayload(value: unknown) {
  const payload = isGatewayRecord(value) ? value : {};
  return compactGatewayRecord({
    node_name: businessString(payload.node_name),
    status: businessString(payload.status),
    started_at: customerTimestamp(payload.started_at),
    finished_at: customerTimestamp(payload.finished_at),
  });
}

function customerTimestamp(value: unknown) {
  if (value instanceof Date) return value.toISOString();
  return businessString(value);
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
    SELECT run_id, status, request, created_at, updated_at
    FROM waje_runtime.analysis_runs
    WHERE run_id = $1
    `,
    [runId],
  );
  const run = runRows.rows[0];
  if (!run) throw new Error("run_not_found");

  const completedPublication = run.status === "completed"
    ? await loadPersistedPublication(runId)
    : null;
  const postExecution = projectPostExecutionStateForCustomer(run.request);
  if (
    run.status === "completed"
    && !completedPublication
    && (!postExecution || postExecution.post_execution_status === "completed")
  ) {
    throw gatewayError("completed_publication_missing");
  }
  const nodeRows = await pool().query(
    `
    SELECT node_name, status, started_at, finished_at
    FROM waje_runtime.run_nodes
    WHERE run_id = $1
    ORDER BY started_at NULLS LAST, finished_at NULLS LAST, node_id
    `,
    [runId],
  );
  const evidenceRows = await pool().query(
    `
    SELECT
      entry_ref,
      evidence_ref,
      task_id,
      outcome_ref,
      binding_record_ref,
      execution_state,
      evidence_kind,
      data_contract_state,
      maximum_claim_strength,
      result_membership_digest,
      completeness_membership_digest,
      created_at
    FROM waje_runtime.capability_evidence_ledger_entries
    WHERE run_attempt_id = $1
    ORDER BY created_at, entry_ref
    `,
    [runId],
  );
  const resultRows = await pool().query(
    `
    SELECT
      query_run.result_ref,
      query_run.query_contract_id AS query_contract_ref,
      query_contract.analysis_contract_id AS analysis_contract_ref,
      query_contract.contract_signature AS query_contract_signature,
      analysis_contract.contract_signature AS analysis_contract_signature,
      query_run.execution_status,
      query_run.query_hash,
      query_run.completeness_report_ref,
      query_authority.record_ref AS query_record_ref,
      query_authority.record_digest AS query_record_digest,
      completeness.record_ref AS completeness_record_ref,
      completeness.report_digest AS completeness_digest,
      completeness.completeness_status,
      completeness.analysis_readiness,
      rows_metadata.row_count,
      CASE
        WHEN jsonb_typeof(
          query_authority.payload #> '{record,source_snapshot_refs}'
        ) = 'array'
        THEN query_authority.payload #> '{record,source_snapshot_refs}'
        ELSE NULL
      END AS snapshot_refs,
      query_run.created_at
    FROM waje_runtime.query_runs query_run
    JOIN waje_runtime.query_contracts query_contract
      ON query_contract.query_contract_id = query_run.query_contract_id
    JOIN waje_runtime.analysis_contracts analysis_contract
      ON analysis_contract.analysis_contract_id = query_contract.analysis_contract_id
    LEFT JOIN waje_runtime.query_execution_authority query_authority
      ON query_authority.run_id = query_run.run_id
     AND query_authority.result_ref = query_run.result_ref
    LEFT JOIN waje_runtime.query_completeness_reports completeness
      ON completeness.run_id = query_run.run_id
     AND completeness.result_ref = query_run.result_ref
    LEFT JOIN waje_runtime.rows_metadata_authority rows_metadata
      ON rows_metadata.rows_ref = query_run.rows_ref
    WHERE query_run.run_id = $1
    ORDER BY query_run.created_at, query_run.result_ref
    `,
    [runId],
  );
  const executionSnapshotRows = await pool().query(
    `
    SELECT
      execution_snapshot_ref,
      authority_context_ref,
      plan_revision_id,
      stop_ref,
      outcome_set_digest,
      evidence_ledger_digest,
      content_digest,
      created_at
    FROM waje_runtime.capability_execution_snapshots
    WHERE run_attempt_id = $1
    ORDER BY created_at, execution_snapshot_ref
    `,
    [runId],
  );
  const auditRows = await pool().query(
    `
    SELECT event_type, payload, created_at
    FROM waje_runtime.audit_events
    WHERE run_id = $1 OR ref = $1
    ORDER BY created_at, audit_id
    `,
    [runId],
  );
  const verifierRows = await pool().query(
    `
    SELECT
      (
        SELECT count(*)
        FROM waje_runtime.claim_verification_decisions
        WHERE run_attempt_id = $1 AND disposition = 'accepted'
      ) AS accepted_claim_count,
      (
        SELECT count(*)
        FROM waje_runtime.claim_verification_decisions
        WHERE run_attempt_id = $1 AND disposition = 'vetoed'
      ) AS vetoed_claim_count,
      (
        SELECT count(*)
        FROM waje_runtime.block_vetoes
        WHERE run_attempt_id = $1
      ) AS vetoed_block_count,
      COALESCE((
        SELECT jsonb_agg(verifier_report_ref ORDER BY verifier_report_ref)
        FROM waje_runtime.claim_verification_reports
        WHERE run_attempt_id = $1
      ), '[]'::jsonb) AS claim_report_refs,
      COALESCE((
        SELECT jsonb_agg(verifier_report_ref ORDER BY verifier_report_ref)
        FROM waje_runtime.block_verification_reports
        WHERE run_attempt_id = $1
      ), '[]'::jsonb) AS block_report_refs
    `,
    [runId],
  );

  return auditTracePayload({
    run,
    customerPublication: completedPublication?.customerPublication ?? null,
    publication: completedPublication?.publication ?? null,
    runNodes: nodeRows.rows,
    evidenceRefs: evidenceRows.rows,
    resultRefs: resultRows.rows,
    executionSnapshots: executionSnapshotRows.rows,
    auditEvents: auditRows.rows,
    verifierStatus: verifierRows.rows[0] ?? {},
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
  if (!sameSet(base.snapshotRefs, candidate.snapshotRefs)) reasons.push("snapshot_mismatch");
  if (!sameSet(base.contractRefs, candidate.contractRefs)) reasons.push("contract_ref_mismatch");
  if (!sameSet(base.queryRefs, candidate.queryRefs)) reasons.push("query_ref_mismatch");
  if (!sameSet(base.resultRefs, candidate.resultRefs)) reasons.push("result_ref_mismatch");
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
  const degradedRuns = await publicationBoundaryRows(limit);
  const blockedRuns = await deliveryFailureRows(limit);
  const verifierFailedRuns = await pool().query(
    `
    SELECT
      r.run_id,
      r.thread_id,
      r.status,
      report.verifier_report_ref,
      jsonb_array_length(report.payload -> 'rejected_block_ids') AS rejected_block_count,
      report.created_at
    FROM waje_runtime.analysis_runs r
    JOIN waje_runtime.block_verification_reports report
      ON report.run_attempt_id = r.run_attempt_id
    WHERE jsonb_array_length(report.payload -> 'rejected_block_ids') > 0
    ORDER BY report.created_at DESC
    LIMIT $1
    `,
    [limit],
  );
  const capabilityErrorRuns = await pool().query(
    `
    SELECT r.run_id, r.thread_id, n.node_name, n.status, n.payload, n.finished_at
    FROM waje_runtime.analysis_runs r
    JOIN waje_runtime.run_nodes n ON n.run_id = r.run_id
    WHERE n.node_name = 'execute_capability_dag'
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
    WHERE n.node_name = 'compile_authoritative_plan'
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

export async function listPersistedAgentRunCandidates(
  limit = 20,
): Promise<PersistedAgentRunCandidates> {
  if (conversationStoreMode() !== "postgres") {
    return { publicationRuns: [], runtimeRuns: [] };
  }
  const client = await pool().connect();
  let transactionOpen = false;
  try {
    await client.query(
      "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
    );
    transactionOpen = true;
    const publicationRuns = await listPersistedPublicationRuns(limit, client);
    const runtimeRuns = await listPersistedRuntimeRuns(limit, client);
    await client.query("COMMIT");
    transactionOpen = false;
    return { publicationRuns, runtimeRuns };
  } catch (error) {
    if (transactionOpen) {
      try {
        await client.query("ROLLBACK");
      } catch {
        // Preserve the read or contract failure that caused the rollback.
      }
    }
    throw error;
  } finally {
    client.release();
  }
}

export async function listPersistedPublicationRuns(
  limit = 20,
  readClient: ReadQueryClient = pool(),
): Promise<PersistedPublicationRun[]> {
  if (conversationStoreMode() !== "postgres") return [];
  const { rows } = await readClient.query(
    `
    SELECT
      r.run_id,
      r.thread_id,
      r.status AS run_status,
      r.request,
      r.created_at AS run_created_at,
      r.updated_at AS run_updated_at,
      customer.customer_payload,
      bundle.bundle_ref AS authority_bundle_ref,
      bundle.bundle_digest AS authority_bundle_digest,
      bundle.sealed_at AS authority_sealed_at,
      publication.publication_ref,
      publication.publication_digest,
      publication.published_at,
      projection.projection_id,
      projection.projection_digest,
      outbox.outbox_ref,
      COALESCE(delivery.status, 'pending') AS delivery_status,
      delivery.attempted_at AS delivery_attempted_at,
      COALESCE(nodes.run_nodes, '[]'::jsonb) AS run_nodes,
      COALESCE(transitions.workflow_transitions, '[]'::jsonb)
        AS workflow_transitions,
      COALESCE(stage_timing.stage_timings, '[]'::jsonb) AS stage_timings,
      COALESCE(evidence.evidence_refs, '[]'::jsonb) AS evidence_refs,
      COALESCE(claims.claim_evidence_links, '[]'::jsonb)
        AS claim_evidence_links,
      plan_trace.accepted_graph,
      verifier.verifier_status,
      COALESCE(human_review.review_state, jsonb_build_object(
        'status', 'pending',
        'evaluationCount', 0
      )) AS human_review
    FROM waje_runtime.publication_customer_payloads customer
    JOIN waje_runtime.analysis_runs r
      ON r.run_attempt_id = customer.run_attempt_id
    JOIN waje_runtime.delivery_outbox_records outbox
      ON outbox.owner_ref = customer.owner_ref
     AND outbox.run_attempt_id = customer.run_attempt_id
     AND outbox.outbox_ref = customer.outbox_ref
    JOIN waje_runtime.publication_revisions publication
      ON publication.owner_ref = customer.owner_ref
     AND publication.run_attempt_id = customer.run_attempt_id
     AND publication.publication_ref = customer.publication_ref
     AND publication.publication_digest = customer.publication_digest
     AND publication.publication_digest = outbox.publication_digest
    JOIN waje_runtime.publication_projections projection
      ON projection.owner_ref = customer.owner_ref
     AND projection.run_attempt_id = customer.run_attempt_id
     AND projection.projection_id = customer.projection_id
     AND projection.projection_digest = customer.projection_digest
     AND projection.projection_digest = outbox.projection_digest
     AND projection.block_verifier_report_ref = publication.block_verifier_report_ref
     AND projection.block_verifier_report_digest = publication.block_verifier_report_digest
    JOIN waje_runtime.block_verification_reports final_block_report
      ON final_block_report.owner_ref = customer.owner_ref
     AND final_block_report.run_attempt_id = customer.run_attempt_id
     AND final_block_report.verifier_report_ref = publication.block_verifier_report_ref
     AND final_block_report.content_digest = publication.block_verifier_report_digest
    JOIN waje_runtime.authority_bundles bundle
      ON bundle.owner_ref = customer.owner_ref
     AND bundle.run_attempt_id = customer.run_attempt_id
     AND bundle.bundle_ref = outbox.authority_bundle_ref
     AND bundle.bundle_digest = outbox.authority_bundle_digest
    JOIN waje_runtime.claim_verification_reports final_claim_report
      ON final_claim_report.owner_ref = customer.owner_ref
     AND final_claim_report.run_attempt_id = customer.run_attempt_id
     AND final_claim_report.verifier_report_ref = bundle.claim_verifier_report_ref
    LEFT JOIN LATERAL (
      SELECT attempt.status, attempt.attempt_number, attempt.attempted_at
      FROM waje_runtime.delivery_attempts attempt
      WHERE attempt.owner_ref = customer.owner_ref
        AND attempt.run_attempt_id = customer.run_attempt_id
        AND attempt.outbox_ref = customer.outbox_ref
      ORDER BY attempt.attempt_number DESC
      LIMIT 1
    ) delivery ON true
    LEFT JOIN LATERAL (
      SELECT jsonb_agg(
        jsonb_build_object(
          'node_name', node_name,
          'status', status,
          'started_at', started_at,
          'finished_at', finished_at
        ) ORDER BY started_at NULLS LAST, finished_at NULLS LAST, node_id
      ) AS run_nodes
      FROM waje_runtime.run_nodes
      WHERE run_id = r.run_id
    ) nodes ON true
    LEFT JOIN LATERAL (
      SELECT
        jsonb_agg(
          jsonb_build_object(
            'attempt_id', transition.attempt_id,
            'transition_id', transition.transition_id,
            'node_name', transition.node_name,
            'provider_ref', transition.provider_ref,
            'model_ref', transition.model_ref,
            'status', transition.status,
            'acceptance_state', transition.acceptance_state,
            'next_transition', transition.next_transition,
            'execution_snapshot_ref', transition_snapshot.execution_snapshot_ref,
            'execution_plan_revision_id', transition_snapshot.plan_revision_id,
            'execution_evidence_entry_refs',
              transition_snapshot.payload -> 'evidence_entry_refs',
            'execution_attempt', transition.execution_attempt,
            'started_at', transition.started_at,
            'finished_at', transition.finished_at
          ) ORDER BY transition.started_at, transition.attempt_id
        ) AS workflow_transitions
      FROM waje_runtime.workflow_transition_attempts transition
      LEFT JOIN waje_runtime.capability_execution_snapshots transition_snapshot
        ON transition_snapshot.run_attempt_id = transition.run_attempt_id
       AND transition_snapshot.execution_snapshot_ref = (
         transition.output_payload #>> '{execution_snapshot,execution_snapshot_ref}'
       )
       AND transition.output_payload -> 'execution_snapshot'
         = transition_snapshot.payload
      WHERE transition.run_attempt_id = r.run_attempt_id
    ) transitions ON true
    LEFT JOIN LATERAL (
      SELECT jsonb_agg(
        jsonb_build_object(
          'transition_attempt_id', timing.transition_attempt_id,
          'stage_name', timing.stage_name,
          'started_at', timing.started_at,
          'finished_at', timing.finished_at,
          'accepted_call_count', timing.accepted_call_count,
          'llm_call_count', timing.llm_call_count,
          'control_call_count', timing.control_call_count,
          'query_call_count', timing.query_call_count,
          'capability_call_count', timing.capability_call_count
        ) ORDER BY timing.started_at, timing.stage_name
      ) AS stage_timings
      FROM (
        SELECT
          binding.transition_attempt_id,
          binding.stage_name,
          min(event.created_at) FILTER (
            WHERE event.status = 'started'
          ) AS started_at,
          max(event.created_at) FILTER (
            WHERE event.status = 'succeeded'
              AND event.success_disposition = 'accepted'
          ) AS finished_at,
          count(DISTINCT binding.accepted_attempt_ref)::integer
            AS accepted_call_count,
          count(DISTINCT binding.accepted_attempt_ref) FILTER (
            WHERE attempt.call_kind IN (
              'conversation_provider',
              'intent_provider',
              'clarification_provider',
              'planner_provider',
              'plan_patch_provider',
              'semantic_provider',
              'narrative_provider'
            )
          )::integer AS llm_call_count,
          count(DISTINCT binding.accepted_attempt_ref) FILTER (
            WHERE attempt.call_kind = 'topic_selection'
          )::integer AS control_call_count,
          count(DISTINCT binding.accepted_attempt_ref) FILTER (
            WHERE attempt.call_kind = 'query'
          )::integer AS query_call_count,
          count(DISTINCT binding.accepted_attempt_ref) FILTER (
            WHERE attempt.call_kind = 'capability'
          )::integer AS capability_call_count
        FROM waje_runtime.durable_stage_attempt_bindings binding
        JOIN waje_runtime.durable_call_acceptances acceptance
          ON acceptance.run_attempt_id = binding.run_attempt_id
         AND acceptance.accepted_attempt_ref = binding.accepted_attempt_ref
        JOIN waje_runtime.durable_call_attempts attempt
          ON attempt.run_attempt_id = acceptance.run_attempt_id
         AND attempt.attempt_ref = acceptance.accepted_attempt_ref
        JOIN waje_runtime.durable_call_attempt_events event
          ON event.run_attempt_id = attempt.run_attempt_id
         AND event.attempt_ref = attempt.attempt_ref
        WHERE binding.run_attempt_id = r.run_attempt_id
        GROUP BY binding.transition_attempt_id, binding.stage_name
        HAVING min(event.created_at) FILTER (
          WHERE event.status = 'started'
        ) IS NOT NULL
          AND max(event.created_at) FILTER (
            WHERE event.status = 'succeeded'
              AND event.success_disposition = 'accepted'
          ) IS NOT NULL
      ) timing
    ) stage_timing ON true
    LEFT JOIN LATERAL (
      SELECT
        jsonb_agg(
          jsonb_build_object(
            'task_id', task.value ->> 'task_id',
            'plan_revision_id', plan.plan_revision_id,
            'capability_id', task.value ->> 'capability_id',
            'task_key', task.value ->> 'task_key',
            'execution_state', CASE
              WHEN execution_closure.transition_attempt_id IS NOT NULL
              THEN 'settled'
              WHEN r.status = 'planned'
                AND NOT execution_activity.has_activity
              THEN 'not_started'
              ELSE 'unsettled'
            END,
            'outcome_ref', task_outcome.outcome_ref,
            'status', task_outcome.status,
            'retryability', task_outcome.retryability,
            'limitation_refs', task_outcome.limitation_refs,
            'failure', task_outcome.failure
          )
          ORDER BY (task.value ->> 'execution_rank')::integer
        ) AS accepted_graph
      FROM waje_runtime.plan_revisions plan
      LEFT JOIN LATERAL (
        SELECT
          transition.attempt_id AS transition_attempt_id,
          snapshot.execution_snapshot_ref,
          snapshot.payload AS snapshot_payload
        FROM waje_runtime.workflow_transition_attempts transition
        JOIN waje_runtime.capability_execution_snapshots snapshot
          ON snapshot.run_attempt_id = transition.run_attempt_id
         AND snapshot.plan_revision_id = plan.plan_revision_id
         AND snapshot.execution_snapshot_ref = (
           transition.output_payload
             #>> '{execution_snapshot,execution_snapshot_ref}'
         )
         AND transition.output_payload -> 'execution_snapshot'
           = snapshot.payload
        WHERE transition.run_attempt_id = plan.run_attempt_id
          AND transition.node_name = 'execute_capability_dag'
          AND transition.acceptance_state = 'accepted'
      ) execution_closure ON true
      CROSS JOIN LATERAL jsonb_array_elements(
        COALESCE(plan.payload -> 'capability_tasks', '[]'::jsonb)
      ) task(value)
      CROSS JOIN LATERAL (
        SELECT (
          EXISTS (
            SELECT 1
            FROM waje_runtime.capability_task_attempts attempted_task
            WHERE attempted_task.run_attempt_id = plan.run_attempt_id
              AND attempted_task.plan_revision_id = plan.plan_revision_id
              AND attempted_task.task_id = task.value ->> 'task_id'
          ) OR EXISTS (
            SELECT 1
            FROM waje_runtime.workflow_transition_attempts attempted_transition
            WHERE attempted_transition.run_attempt_id = plan.run_attempt_id
              AND attempted_transition.node_name = 'execute_capability_dag'
          )
        ) AS has_activity
      ) execution_activity
      LEFT JOIN LATERAL (
        SELECT
          outcome.outcome_ref,
          outcome.status,
          outcome.retryability,
          outcome.payload -> 'limitation_refs' AS limitation_refs,
          CASE
            WHEN failure.failure_ref IS NULL THEN NULL
            ELSE jsonb_build_object(
              'layer', failure.layer,
              'kind', failure.kind,
              'integrity_level', failure.integrity_level,
              'business_boundary', failure.payload ->> 'business_boundary'
            )
          END AS failure
        FROM waje_runtime.capability_outcomes outcome
        JOIN waje_runtime.capability_task_attempts attempt
          ON attempt.attempt_id = outcome.attempt_id
         AND attempt.run_attempt_id = outcome.run_attempt_id
         AND attempt.plan_revision_id = outcome.plan_revision_id
         AND attempt.task_id = outcome.task_id
        JOIN waje_runtime.durable_stage_attempt_bindings binding
          ON binding.run_attempt_id = attempt.run_attempt_id
         AND binding.accepted_attempt_ref = attempt.attempt_id
         AND binding.transition_attempt_id
           = execution_closure.transition_attempt_id
         AND binding.stage_name = 'execute_capability_dag'
        LEFT JOIN waje_runtime.capability_failure_records failure
          ON failure.failure_ref = outcome.failure_ref
         AND failure.run_attempt_id = outcome.run_attempt_id
         AND failure.plan_revision_id = outcome.plan_revision_id
         AND failure.task_id = outcome.task_id
         AND failure.attempt_id = outcome.attempt_id
         AND failure.retryability = outcome.retryability
        WHERE outcome.run_attempt_id = plan.run_attempt_id
          AND outcome.plan_revision_id = plan.plan_revision_id
          AND outcome.task_id = task.value ->> 'task_id'
          AND (execution_closure.snapshot_payload -> 'outcome_refs')
            ? outcome.outcome_ref
          AND (
            outcome.failure_ref IS NULL
            OR failure.failure_ref IS NOT NULL
          )
      ) task_outcome ON true
      WHERE plan.run_attempt_id = r.run_attempt_id
        AND NOT EXISTS (
          SELECT 1
          FROM waje_runtime.plan_revision_supersessions supersession
          WHERE supersession.superseded_plan_revision_id = plan.plan_revision_id
        )
    ) plan_trace ON true
    LEFT JOIN LATERAL (
      SELECT jsonb_agg(
        jsonb_build_object(
          'entry_ref', entry.entry_ref,
          'evidence_ref', entry.evidence_ref,
          'plan_revision_id', entry.plan_revision_id,
          'plan_state', CASE
            WHEN EXISTS (
              SELECT 1
              FROM waje_runtime.plan_revision_supersessions supersession
              WHERE supersession.superseded_plan_revision_id = entry.plan_revision_id
            ) THEN 'superseded'
            ELSE 'active'
          END,
          'task_id', entry.task_id,
          'capability_id', evidence_task.capability_id,
          'binding_state', CASE
            WHEN execution_snapshot.execution_snapshot_ref IS NOT NULL
            THEN 'bound'
            ELSE 'unsettled'
          END,
          'execution_transition_attempt_id', CASE
            WHEN execution_snapshot.execution_snapshot_ref IS NOT NULL
            THEN execution_transition.attempt_id
            ELSE NULL
          END,
          'outcome_ref', entry.outcome_ref,
          'execution_state', entry.execution_state,
          'evidence_kind', entry.evidence_kind,
          'data_contract_state', entry.data_contract_state,
          'maximum_claim_strength', entry.maximum_claim_strength,
          'limitation_refs', entry.payload -> 'limitation_refs',
          'created_at', entry.created_at
        ) ORDER BY entry.created_at, entry.entry_ref
      ) AS evidence_refs
      FROM waje_runtime.capability_evidence_ledger_entries entry
      JOIN waje_runtime.plan_revisions evidence_plan
        ON evidence_plan.run_attempt_id = entry.run_attempt_id
       AND evidence_plan.plan_revision_id = entry.plan_revision_id
      LEFT JOIN waje_runtime.capability_outcomes evidence_outcome
        ON evidence_outcome.outcome_ref = entry.outcome_ref
       AND evidence_outcome.run_attempt_id = entry.run_attempt_id
       AND evidence_outcome.plan_revision_id = entry.plan_revision_id
       AND evidence_outcome.task_id = entry.task_id
      LEFT JOIN waje_runtime.capability_task_attempts evidence_attempt
        ON evidence_attempt.attempt_id = evidence_outcome.attempt_id
       AND evidence_attempt.run_attempt_id = evidence_outcome.run_attempt_id
       AND evidence_attempt.plan_revision_id = evidence_outcome.plan_revision_id
       AND evidence_attempt.task_id = evidence_outcome.task_id
      LEFT JOIN waje_runtime.durable_stage_attempt_bindings execution_binding
        ON execution_binding.run_attempt_id = evidence_attempt.run_attempt_id
       AND execution_binding.accepted_attempt_ref = evidence_attempt.attempt_id
       AND execution_binding.stage_name = 'execute_capability_dag'
      LEFT JOIN waje_runtime.workflow_transition_attempts execution_transition
        ON execution_transition.run_attempt_id = execution_binding.run_attempt_id
       AND execution_transition.attempt_id = execution_binding.transition_attempt_id
       AND execution_transition.node_name = 'execute_capability_dag'
       AND execution_transition.acceptance_state = 'accepted'
      LEFT JOIN waje_runtime.capability_execution_snapshots execution_snapshot
        ON execution_snapshot.run_attempt_id = entry.run_attempt_id
       AND execution_snapshot.plan_revision_id = entry.plan_revision_id
       AND execution_snapshot.execution_snapshot_ref = (
         execution_transition.output_payload #>> '{execution_snapshot,execution_snapshot_ref}'
       )
       AND execution_transition.output_payload -> 'execution_snapshot'
         = execution_snapshot.payload
       AND (execution_snapshot.payload -> 'evidence_entry_refs') ? entry.entry_ref
      LEFT JOIN LATERAL (
        SELECT task.value ->> 'capability_id' AS capability_id
        FROM jsonb_array_elements(
          evidence_plan.payload -> 'capability_tasks'
        ) task(value)
        WHERE task.value ->> 'task_id' = entry.task_id
      ) evidence_task ON true
      WHERE entry.run_attempt_id = r.run_attempt_id
    ) evidence ON true
    LEFT JOIN LATERAL (
      SELECT jsonb_agg(
        jsonb_build_object(
          'claim_ref', revision.claim_ref,
          'claim_class', revision.claim_class,
          'claim_status', revision.claim_status,
          'evidence_refs', COALESCE(link.evidence_refs, '[]'::jsonb)
        ) ORDER BY revision.created_at, revision.claim_ref
      ) AS claim_evidence_links
      FROM waje_runtime.claim_revisions revision
      LEFT JOIN LATERAL (
        SELECT jsonb_agg(
          evidence_entry.evidence_ref ORDER BY evidence_entry.evidence_ref
        ) AS evidence_refs
        FROM waje_runtime.claim_support_edges edge
        JOIN waje_runtime.capability_evidence_ledger_entries evidence_entry
          ON evidence_entry.run_attempt_id = edge.run_attempt_id
         AND evidence_entry.entry_ref = edge.source_ref
        WHERE edge.run_attempt_id = revision.run_attempt_id
          AND edge.target_claim_key = revision.claim_key
          AND edge.source_type = 'evidence'
      ) link ON true
      WHERE revision.run_attempt_id = r.run_attempt_id
        AND revision.claim_status = 'verified'
    ) claims ON true
    LEFT JOIN LATERAL (
      SELECT jsonb_build_object(
        'acceptedClaimCount', jsonb_array_length(
          final_claim_report.payload -> 'accepted_claim_refs'
        ),
        'vetoedClaimCount', jsonb_array_length(
          final_claim_report.payload -> 'rejected_claim_refs'
        ),
        'acceptedBlockCount', jsonb_array_length(
          final_block_report.payload -> 'accepted_block_ids'
        ),
        'rejectedBlockCount', jsonb_array_length(
          final_block_report.payload -> 'rejected_block_ids'
        ),
        'vetoedBlockCount', (
          SELECT count(*)
          FROM waje_runtime.block_vetoes veto
          WHERE veto.run_attempt_id = r.run_attempt_id
            AND veto.verification_attempt_ref = final_block_report.verification_attempt_ref
        ),
        'claimReportRefs', jsonb_build_array(
          final_claim_report.verifier_report_ref
        ),
        'blockReportRefs', jsonb_build_array(
          final_block_report.verifier_report_ref
        ),
        'verifiedAt', GREATEST(
          final_claim_report.created_at,
          final_block_report.created_at
        )
      ) AS verifier_status
    ) verifier ON true
    LEFT JOIN LATERAL (
      SELECT jsonb_build_object(
        'status', CASE
          WHEN latest.result = 'request_independent_narrative_attempt'
          THEN 'revision_requested'
          ELSE 'reviewed'
        END,
        'evaluationCount', (
          SELECT count(*)::integer
          FROM waje_runtime.insight_quality_evaluations counted
          WHERE counted.owner_ref = customer.owner_ref
            AND counted.run_attempt_id = customer.run_attempt_id
            AND counted.source_publication_ref = publication.publication_ref
        ),
        'latest', jsonb_build_object(
          'reviewerRef', latest.reviewer_ref,
          'scores', latest.scores,
          'humanReasons', latest.human_reasons,
          'result', latest.result,
          'reviewedAt', latest.reviewed_at
        )
      ) AS review_state
      FROM waje_runtime.insight_quality_evaluations latest
      WHERE latest.owner_ref = customer.owner_ref
        AND latest.run_attempt_id = customer.run_attempt_id
        AND latest.source_publication_ref = publication.publication_ref
      ORDER BY latest.reviewed_at DESC, latest.evaluation_ref DESC
      LIMIT 1
    ) human_review ON true
    WHERE (
      COALESCE(delivery.status, 'pending') = 'pending'
      AND r.status = 'narrative_ready'
      AND r.request ->> 'post_execution_status' = 'narrative_ready'
      AND r.request ->> 'publication_status' = 'ready'
      AND r.request ->> 'delivery_status' = 'persisted'
    ) OR (
      delivery.status = 'published'
      AND r.status = 'completed'
      AND r.request ->> 'post_execution_status' = 'completed'
      AND r.request ->> 'publication_status' = 'published'
      AND r.request ->> 'delivery_status' = 'published'
    ) OR (
      delivery.status = 'retryable_failed'
      AND r.status = 'completed'
      AND r.request ->> 'post_execution_status' = 'delivery_retryable_failed'
      AND r.request ->> 'publication_status' = 'ready'
      AND r.request ->> 'delivery_status' = 'retryable_failed'
    ) OR (
      delivery.status = 'permanently_failed'
      AND r.status = 'completed'
      AND r.request ->> 'post_execution_status' = 'delivery_permanently_failed'
      AND r.request ->> 'publication_status' = 'ready'
      AND r.request ->> 'delivery_status' = 'permanently_failed'
    )
    ORDER BY customer.created_at DESC
    LIMIT $1
    `,
    [limit],
  );
  return rows.map((row) => {
    const request = row.request ?? {};
    const customerPublication = requireCustomerPublication(row.customer_payload);
    const publication = publicationRefsFromRow(row);
    return {
      runId: row.run_id,
      threadId: row.thread_id,
      runStatus: row.run_status,
      question: String(request.question ?? request.user_message ?? ""),
      request,
      customerPublication,
      publication,
      runNodes: Array.isArray(row.run_nodes) ? row.run_nodes : [],
      workflowTransitions: Array.isArray(row.workflow_transitions)
        ? row.workflow_transitions
        : [],
      stageTimings: Array.isArray(row.stage_timings) ? row.stage_timings : [],
      evidenceRefs: Array.isArray(row.evidence_refs) ? row.evidence_refs : [],
      claimEvidenceLinks: Array.isArray(row.claim_evidence_links)
        ? row.claim_evidence_links
        : [],
      acceptedGraph: Array.isArray(row.accepted_graph)
        ? row.accepted_graph.map(requiredAcceptedGraphRecord)
        : [],
      verifierStatus: isGatewayRecord(row.verifier_status)
        ? row.verifier_status
        : {},
      humanReview: isGatewayRecord(row.human_review)
        ? row.human_review
        : { status: "pending", evaluationCount: 0 },
      createdAt: row.run_created_at,
      updatedAt: row.run_updated_at,
    } satisfies PersistedPublicationRun;
  });
}

export async function loadPersistedPublication(runId: string) {
  const { rows } = await pool().query(
    `
    SELECT
      customer.customer_payload,
      bundle.bundle_ref AS authority_bundle_ref,
      bundle.bundle_digest AS authority_bundle_digest,
      bundle.sealed_at AS authority_sealed_at,
      publication.publication_ref,
      publication.publication_digest,
      publication.published_at,
      projection.projection_id,
      projection.projection_digest,
      outbox.outbox_ref,
      delivery.status AS delivery_status,
      delivery.attempted_at AS delivery_attempted_at
    FROM waje_runtime.publication_customer_payloads customer
    JOIN waje_runtime.delivery_outbox_records outbox
      ON outbox.owner_ref = customer.owner_ref
     AND outbox.run_attempt_id = customer.run_attempt_id
     AND outbox.outbox_ref = customer.outbox_ref
    JOIN waje_runtime.publication_revisions publication
      ON publication.owner_ref = customer.owner_ref
     AND publication.run_attempt_id = customer.run_attempt_id
     AND publication.publication_ref = customer.publication_ref
     AND publication.publication_digest = customer.publication_digest
     AND publication.publication_digest = outbox.publication_digest
    JOIN waje_runtime.publication_projections projection
      ON projection.owner_ref = customer.owner_ref
     AND projection.run_attempt_id = customer.run_attempt_id
     AND projection.projection_id = customer.projection_id
     AND projection.projection_digest = customer.projection_digest
     AND projection.projection_digest = outbox.projection_digest
    JOIN waje_runtime.authority_bundles bundle
      ON bundle.owner_ref = customer.owner_ref
     AND bundle.run_attempt_id = customer.run_attempt_id
     AND bundle.bundle_ref = outbox.authority_bundle_ref
     AND bundle.bundle_digest = outbox.authority_bundle_digest
    JOIN LATERAL (
      SELECT attempt.status, attempt.attempt_number, attempt.attempted_at
      FROM waje_runtime.delivery_attempts attempt
      WHERE attempt.owner_ref = customer.owner_ref
        AND attempt.run_attempt_id = customer.run_attempt_id
        AND attempt.outbox_ref = customer.outbox_ref
      ORDER BY attempt.attempt_number DESC
      LIMIT 1
    ) delivery ON true
    WHERE customer.run_attempt_id = $1
    ORDER BY publication.revision DESC, customer.created_at DESC
    LIMIT 1
    `,
    [runId],
  );
  const row = rows[0];
  if (!row) return null;
  return {
    customerPublication: requireCustomerPublication(row.customer_payload),
    publication: publicationRefsFromRow(row),
  };
}

export async function listPersistedRuntimeRuns(
  limit = 20,
  readClient: ReadQueryClient = pool(),
): Promise<PersistedRuntimeRun[]> {
  if (conversationStoreMode() !== "postgres") return [];
  const { rows } = await readClient.query(
    `
    SELECT
      r.run_id,
      r.thread_id,
      r.status AS run_status,
      r.request,
      r.created_at AS run_created_at,
      r.updated_at AS run_updated_at,
      COALESCE(nodes.run_nodes, '[]'::jsonb) AS run_nodes,
      COALESCE(transitions.workflow_transitions, '[]'::jsonb)
        AS workflow_transitions,
      COALESCE(stage_timing.stage_timings, '[]'::jsonb) AS stage_timings,
      COALESCE(evidence.evidence_refs, '[]'::jsonb) AS evidence_refs,
      plan_trace.accepted_graph
    FROM waje_runtime.analysis_runs r
    LEFT JOIN LATERAL (
      SELECT jsonb_agg(
        jsonb_build_object(
          'node_name', node.node_name,
          'status', node.status,
          'started_at', node.started_at,
          'finished_at', node.finished_at
        ) ORDER BY node.started_at NULLS LAST,
          node.finished_at NULLS LAST,
          node.node_id
      ) AS run_nodes
      FROM waje_runtime.run_nodes node
      WHERE node.run_id = r.run_id
    ) nodes ON true
    LEFT JOIN LATERAL (
      SELECT jsonb_agg(
        jsonb_build_object(
          'attempt_id', transition.attempt_id,
          'transition_id', transition.transition_id,
          'node_name', transition.node_name,
          'provider_ref', transition.provider_ref,
          'model_ref', transition.model_ref,
          'status', transition.status,
          'acceptance_state', transition.acceptance_state,
          'next_transition', transition.next_transition,
          'execution_snapshot_ref', transition_snapshot.execution_snapshot_ref,
          'execution_plan_revision_id', transition_snapshot.plan_revision_id,
          'execution_evidence_entry_refs',
            transition_snapshot.payload -> 'evidence_entry_refs',
          'execution_attempt', transition.execution_attempt,
          'started_at', transition.started_at,
          'finished_at', transition.finished_at
        ) ORDER BY transition.started_at, transition.attempt_id
      ) AS workflow_transitions
      FROM waje_runtime.workflow_transition_attempts transition
      LEFT JOIN waje_runtime.capability_execution_snapshots transition_snapshot
        ON transition_snapshot.run_attempt_id = transition.run_attempt_id
       AND transition_snapshot.execution_snapshot_ref = (
         transition.output_payload #>> '{execution_snapshot,execution_snapshot_ref}'
       )
       AND transition.output_payload -> 'execution_snapshot'
         = transition_snapshot.payload
      WHERE transition.run_attempt_id = r.run_attempt_id
    ) transitions ON true
    LEFT JOIN LATERAL (
      SELECT jsonb_agg(
        jsonb_build_object(
          'transition_attempt_id', timing.transition_attempt_id,
          'stage_name', timing.stage_name,
          'started_at', timing.started_at,
          'finished_at', timing.finished_at,
          'accepted_call_count', timing.accepted_call_count,
          'llm_call_count', timing.llm_call_count,
          'control_call_count', timing.control_call_count,
          'query_call_count', timing.query_call_count,
          'capability_call_count', timing.capability_call_count
        ) ORDER BY timing.started_at, timing.stage_name
      ) AS stage_timings
      FROM (
        SELECT
          binding.transition_attempt_id,
          binding.stage_name,
          min(event.created_at) FILTER (
            WHERE event.status = 'started'
          ) AS started_at,
          max(event.created_at) FILTER (
            WHERE event.status = 'succeeded'
              AND event.success_disposition = 'accepted'
          ) AS finished_at,
          count(DISTINCT binding.accepted_attempt_ref)::integer
            AS accepted_call_count,
          count(DISTINCT binding.accepted_attempt_ref) FILTER (
            WHERE attempt.call_kind IN (
              'conversation_provider',
              'intent_provider',
              'clarification_provider',
              'planner_provider',
              'plan_patch_provider',
              'semantic_provider',
              'narrative_provider'
            )
          )::integer AS llm_call_count,
          count(DISTINCT binding.accepted_attempt_ref) FILTER (
            WHERE attempt.call_kind = 'topic_selection'
          )::integer AS control_call_count,
          count(DISTINCT binding.accepted_attempt_ref) FILTER (
            WHERE attempt.call_kind = 'query'
          )::integer AS query_call_count,
          count(DISTINCT binding.accepted_attempt_ref) FILTER (
            WHERE attempt.call_kind = 'capability'
          )::integer AS capability_call_count
        FROM waje_runtime.durable_stage_attempt_bindings binding
        JOIN waje_runtime.durable_call_acceptances acceptance
          ON acceptance.run_attempt_id = binding.run_attempt_id
         AND acceptance.accepted_attempt_ref = binding.accepted_attempt_ref
        JOIN waje_runtime.durable_call_attempts attempt
          ON attempt.run_attempt_id = acceptance.run_attempt_id
         AND attempt.attempt_ref = acceptance.accepted_attempt_ref
        JOIN waje_runtime.durable_call_attempt_events event
          ON event.run_attempt_id = attempt.run_attempt_id
         AND event.attempt_ref = attempt.attempt_ref
        WHERE binding.run_attempt_id = r.run_attempt_id
        GROUP BY binding.transition_attempt_id, binding.stage_name
        HAVING min(event.created_at) FILTER (
          WHERE event.status = 'started'
        ) IS NOT NULL
          AND max(event.created_at) FILTER (
            WHERE event.status = 'succeeded'
              AND event.success_disposition = 'accepted'
          ) IS NOT NULL
      ) timing
    ) stage_timing ON true
    LEFT JOIN LATERAL (
      SELECT
        jsonb_agg(
          jsonb_build_object(
            'task_id', task.value ->> 'task_id',
            'plan_revision_id', plan.plan_revision_id,
            'capability_id', task.value ->> 'capability_id',
            'task_key', task.value ->> 'task_key',
            'execution_state', CASE
              WHEN execution_closure.transition_attempt_id IS NOT NULL
              THEN 'settled'
              WHEN r.status = 'planned'
                AND NOT execution_activity.has_activity
              THEN 'not_started'
              ELSE 'unsettled'
            END,
            'outcome_ref', task_outcome.outcome_ref,
            'status', task_outcome.status,
            'retryability', task_outcome.retryability,
            'limitation_refs', task_outcome.limitation_refs,
            'failure', task_outcome.failure
          )
          ORDER BY (task.value ->> 'execution_rank')::integer
        ) AS accepted_graph
      FROM waje_runtime.plan_revisions plan
      LEFT JOIN LATERAL (
        SELECT
          transition.attempt_id AS transition_attempt_id,
          snapshot.execution_snapshot_ref,
          snapshot.payload AS snapshot_payload
        FROM waje_runtime.workflow_transition_attempts transition
        JOIN waje_runtime.capability_execution_snapshots snapshot
          ON snapshot.run_attempt_id = transition.run_attempt_id
         AND snapshot.plan_revision_id = plan.plan_revision_id
         AND snapshot.execution_snapshot_ref = (
           transition.output_payload
             #>> '{execution_snapshot,execution_snapshot_ref}'
         )
         AND transition.output_payload -> 'execution_snapshot'
           = snapshot.payload
        WHERE transition.run_attempt_id = plan.run_attempt_id
          AND transition.node_name = 'execute_capability_dag'
          AND transition.acceptance_state = 'accepted'
      ) execution_closure ON true
      CROSS JOIN LATERAL jsonb_array_elements(
        COALESCE(plan.payload -> 'capability_tasks', '[]'::jsonb)
      ) task(value)
      CROSS JOIN LATERAL (
        SELECT (
          EXISTS (
            SELECT 1
            FROM waje_runtime.capability_task_attempts attempted_task
            WHERE attempted_task.run_attempt_id = plan.run_attempt_id
              AND attempted_task.plan_revision_id = plan.plan_revision_id
              AND attempted_task.task_id = task.value ->> 'task_id'
          ) OR EXISTS (
            SELECT 1
            FROM waje_runtime.workflow_transition_attempts attempted_transition
            WHERE attempted_transition.run_attempt_id = plan.run_attempt_id
              AND attempted_transition.node_name = 'execute_capability_dag'
          )
        ) AS has_activity
      ) execution_activity
      LEFT JOIN LATERAL (
        SELECT
          outcome.outcome_ref,
          outcome.status,
          outcome.retryability,
          outcome.payload -> 'limitation_refs' AS limitation_refs,
          CASE
            WHEN failure.failure_ref IS NULL THEN NULL
            ELSE jsonb_build_object(
              'layer', failure.layer,
              'kind', failure.kind,
              'integrity_level', failure.integrity_level,
              'business_boundary', failure.payload ->> 'business_boundary'
            )
          END AS failure
        FROM waje_runtime.capability_outcomes outcome
        JOIN waje_runtime.capability_task_attempts attempt
          ON attempt.attempt_id = outcome.attempt_id
         AND attempt.run_attempt_id = outcome.run_attempt_id
         AND attempt.plan_revision_id = outcome.plan_revision_id
         AND attempt.task_id = outcome.task_id
        JOIN waje_runtime.durable_stage_attempt_bindings binding
          ON binding.run_attempt_id = attempt.run_attempt_id
         AND binding.accepted_attempt_ref = attempt.attempt_id
         AND binding.transition_attempt_id
           = execution_closure.transition_attempt_id
         AND binding.stage_name = 'execute_capability_dag'
        LEFT JOIN waje_runtime.capability_failure_records failure
          ON failure.failure_ref = outcome.failure_ref
         AND failure.run_attempt_id = outcome.run_attempt_id
         AND failure.plan_revision_id = outcome.plan_revision_id
         AND failure.task_id = outcome.task_id
         AND failure.attempt_id = outcome.attempt_id
         AND failure.retryability = outcome.retryability
        WHERE outcome.run_attempt_id = plan.run_attempt_id
          AND outcome.plan_revision_id = plan.plan_revision_id
          AND outcome.task_id = task.value ->> 'task_id'
          AND (execution_closure.snapshot_payload -> 'outcome_refs')
            ? outcome.outcome_ref
          AND (
            outcome.failure_ref IS NULL
            OR failure.failure_ref IS NOT NULL
          )
      ) task_outcome ON true
      WHERE plan.run_attempt_id = r.run_attempt_id
        AND NOT EXISTS (
          SELECT 1
          FROM waje_runtime.plan_revision_supersessions supersession
          WHERE supersession.superseded_plan_revision_id = plan.plan_revision_id
        )
    ) plan_trace ON true
    LEFT JOIN LATERAL (
      SELECT jsonb_agg(
        jsonb_build_object(
          'entry_ref', entry.entry_ref,
          'evidence_ref', entry.evidence_ref,
          'plan_revision_id', entry.plan_revision_id,
          'plan_state', CASE
            WHEN EXISTS (
              SELECT 1
              FROM waje_runtime.plan_revision_supersessions supersession
              WHERE supersession.superseded_plan_revision_id = entry.plan_revision_id
            ) THEN 'superseded'
            ELSE 'active'
          END,
          'task_id', entry.task_id,
          'capability_id', evidence_task.capability_id,
          'binding_state', CASE
            WHEN execution_snapshot.execution_snapshot_ref IS NOT NULL
            THEN 'bound'
            ELSE 'unsettled'
          END,
          'execution_transition_attempt_id', CASE
            WHEN execution_snapshot.execution_snapshot_ref IS NOT NULL
            THEN execution_transition.attempt_id
            ELSE NULL
          END,
          'outcome_ref', entry.outcome_ref,
          'execution_state', entry.execution_state,
          'evidence_kind', entry.evidence_kind,
          'data_contract_state', entry.data_contract_state,
          'maximum_claim_strength', entry.maximum_claim_strength,
          'limitation_refs', entry.payload -> 'limitation_refs',
          'created_at', entry.created_at
        ) ORDER BY entry.created_at, entry.entry_ref
      ) AS evidence_refs
      FROM waje_runtime.capability_evidence_ledger_entries entry
      JOIN waje_runtime.plan_revisions evidence_plan
        ON evidence_plan.run_attempt_id = entry.run_attempt_id
       AND evidence_plan.plan_revision_id = entry.plan_revision_id
      LEFT JOIN waje_runtime.capability_outcomes evidence_outcome
        ON evidence_outcome.outcome_ref = entry.outcome_ref
       AND evidence_outcome.run_attempt_id = entry.run_attempt_id
       AND evidence_outcome.plan_revision_id = entry.plan_revision_id
       AND evidence_outcome.task_id = entry.task_id
      LEFT JOIN waje_runtime.capability_task_attempts evidence_attempt
        ON evidence_attempt.attempt_id = evidence_outcome.attempt_id
       AND evidence_attempt.run_attempt_id = evidence_outcome.run_attempt_id
       AND evidence_attempt.plan_revision_id = evidence_outcome.plan_revision_id
       AND evidence_attempt.task_id = evidence_outcome.task_id
      LEFT JOIN waje_runtime.durable_stage_attempt_bindings execution_binding
        ON execution_binding.run_attempt_id = evidence_attempt.run_attempt_id
       AND execution_binding.accepted_attempt_ref = evidence_attempt.attempt_id
       AND execution_binding.stage_name = 'execute_capability_dag'
      LEFT JOIN waje_runtime.workflow_transition_attempts execution_transition
        ON execution_transition.run_attempt_id = execution_binding.run_attempt_id
       AND execution_transition.attempt_id = execution_binding.transition_attempt_id
       AND execution_transition.node_name = 'execute_capability_dag'
       AND execution_transition.acceptance_state = 'accepted'
      LEFT JOIN waje_runtime.capability_execution_snapshots execution_snapshot
        ON execution_snapshot.run_attempt_id = entry.run_attempt_id
       AND execution_snapshot.plan_revision_id = entry.plan_revision_id
       AND execution_snapshot.execution_snapshot_ref = (
         execution_transition.output_payload #>> '{execution_snapshot,execution_snapshot_ref}'
       )
       AND execution_transition.output_payload -> 'execution_snapshot'
         = execution_snapshot.payload
       AND (execution_snapshot.payload -> 'evidence_entry_refs') ? entry.entry_ref
      LEFT JOIN LATERAL (
        SELECT task.value ->> 'capability_id' AS capability_id
        FROM jsonb_array_elements(
          evidence_plan.payload -> 'capability_tasks'
        ) task(value)
        WHERE task.value ->> 'task_id' = entry.task_id
      ) evidence_task ON true
      WHERE entry.run_attempt_id = r.run_attempt_id
    ) evidence ON true
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
      runNodes: Array.isArray(row.run_nodes) ? row.run_nodes : [],
      workflowTransitions: Array.isArray(row.workflow_transitions)
        ? row.workflow_transitions
        : [],
      stageTimings: Array.isArray(row.stage_timings) ? row.stage_timings : [],
      evidenceRefs: Array.isArray(row.evidence_refs) ? row.evidence_refs : [],
      acceptedGraph: Array.isArray(row.accepted_graph)
        ? row.accepted_graph.map(requiredAcceptedGraphRecord)
        : [],
      createdAt: row.run_created_at,
      updatedAt: row.run_updated_at,
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

export async function customerJsonError(
  error: unknown,
  context: {
    actorId?: string;
    threadId?: string;
    runId?: string;
  } = {},
) {
  const internalCode = error instanceof GatewayRuntimeError
    ? error.code
    : error instanceof Error
      ? error.message
      : "unknown_error";
  const technicalDetailRef = `customer-error-${crypto.randomUUID()}`;
  const projected = customerErrorProjection(internalCode);
  const { httpStatus, ...publicError } = projected;
  console.error(`[${technicalDetailRef}] ${internalCode}`, error);
  try {
    if (conversationStoreMode() === "postgres") {
      await audit("customer_request_failed", {
        ...context,
        ref: technicalDetailRef,
        payload: { internalCode },
      });
    } else {
      memoryStore().auditEvents.push({
        eventType: "customer_request_failed",
        ...context,
        ref: technicalDetailRef,
        payload: { internalCode },
      });
    }
  } catch (auditError) {
    console.error(`[${technicalDetailRef}] customer_error_audit_failed`, auditError);
  }
  return Response.json(
    {
      error: publicError,
      transport: { technicalDetailRef },
    },
    { status: httpStatus },
  );
}

function customerErrorProjection(code: string) {
  if (code === "customer_identity_required" || code === "customer_identity_invalid") {
    return {
      code: "sign_in_required" as const,
      title: "需要重新登录",
      message: "当前身份无法继续访问这项分析。",
      recovery: "sign_in" as const,
      httpStatus: 401,
    };
  }
  if (code.endsWith("_not_found") || code.endsWith("_owner_mismatch")) {
    return {
      code: "analysis_not_found" as const,
      title: "无法打开这项分析",
      message: "这项分析不存在，或当前账号没有访问权限。",
      recovery: "new_analysis" as const,
      httpStatus: code.endsWith("_not_found") ? 404 : 403,
    };
  }
  if (code === "clarification_source_not_waiting") {
    return {
      code: "action_no_longer_available" as const,
      title: "这项确认已不可提交",
      message: "运行状态已经变化。刷新后将以最新持久化状态为准。",
      recovery: "refresh" as const,
      httpStatus: 409,
    };
  }
  if (code === "run_dispatch_active_conflict" || code === "run_dispatch_conflict") {
    return {
      code: "action_in_progress" as const,
      title: "正在确认提交结果",
      message: "同一项操作仍在处理中。刷新后会恢复服务器已确认的结果。",
      recovery: "refresh" as const,
      httpStatus: 409,
    };
  }
  if (CUSTOMER_INPUT_ERROR_CODES.has(code)) {
    return {
      code: "request_invalid" as const,
      title: "提交内容无法处理",
      message: "请检查当前输入后再次提交。",
      recovery: "retry" as const,
      httpStatus: 400,
    };
  }
  return {
    code: "analysis_unavailable" as const,
    title: "分析服务暂时无法完成请求",
    message: "真实故障已经记录。请稍后重试；若问题持续，可联系支持定位。",
    recovery: "retry" as const,
    httpStatus: errorHttpStatus(code),
  };
}

const CUSTOMER_INPUT_ERROR_CODES = new Set([
  "thread_request_invalid",
  "thread_owner_input_forbidden",
  "message_request_invalid",
  "message_required",
  "clarification_request_invalid",
  "clarification_answer_required",
  "clarification_selected_option_invalid",
  "topic_choice_answer_invalid",
  "topic_choice_answer_message_mismatch",
  "topic_choice_input_conflict",
  "topic_selection_invalid",
  "run_dispatch_request_invalid",
  "run_dispatch_request_identity_required",
  "run_dispatch_request_identity_invalid",
  "run_dispatch_request_identity_conflict",
]);

function errorHttpStatus(code: string) {
  const status = gatewayHttpStatus(code);
  return status >= 400 && status < 600 ? status : 500;
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
      "run_dispatch_active_conflict",
      "run_dispatch_conflict",
      "run_dispatch_lease_lost",
    ].includes(code)
  ) {
    return 409;
  }
  if (
    code === "thread_owner_mismatch"
    || code === "run_owner_mismatch"
    || code === "memory_owner_mismatch"
  ) {
    return 403;
  }
  if (
    code === "clarification_answer_required"
    || code === "clarification_selected_option_invalid"
    || code.startsWith("run_dispatch_request_identity_")
    || code === "run_dispatch_request_invalid"
    || code === "run_dispatch_producer_invalid"
    || code === "message_request_invalid"
    || code === "topic_selection_invalid"
    || code === "topic_choice_answer_invalid"
    || code === "topic_choice_answer_message_mismatch"
    || code === "topic_choice_input_conflict"
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

function normalizeRunDispatchInput(input: RunDispatchInput) {
  if (
    input.producerKind !== "thread_message"
    && input.producerKind !== "clarification_resolution"
  ) {
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
  const threadId = input.threadId.trim();
  const scopeRef = input.scopeRef.trim();
  const requestIdentity = input.requestIdentity.trim();
  const text = input.text.trim();
  const runId = input.producerKind === "clarification_resolution"
    && isNonEmptyGatewayString(input.runId)
    ? input.runId.trim()
    : "";
  if (
    requestIdentity.length > 256
    || (input.producerKind === "thread_message" && scopeRef !== threadId)
    || (
      input.producerKind === "clarification_resolution"
      && (scopeRef !== runId || !runId)
    )
  ) {
    throw gatewayError("run_dispatch_request_invalid");
  }
  const rawRequestPayload = input.requestPayload ?? { message: text };
  if (
    rawRequestPayload === null
    || typeof rawRequestPayload !== "object"
    || Array.isArray(rawRequestPayload)
  ) {
    throw gatewayError("run_dispatch_request_invalid");
  }
  const requestPayload = canonicalRunDispatchRecord(rawRequestPayload);
  if (requestPayload.message !== text) {
    throw gatewayError("run_dispatch_request_invalid");
  }
  const actorId = normalizeActorId(input.actorId);
  return {
    producerKind: input.producerKind,
    scopeRef,
    requestIdentity,
    threadId,
    runId,
    text,
    actorId,
    requestPayload,
  };
}

function runDispatchRequestDigest(input: ReturnType<typeof normalizeRunDispatchInput>) {
  // Keep this snake_case envelope byte-compatible with Python canonical_digest.
  // The idempotency identity is carried by the unique tuple, outside this digest.
  return createHash("sha256").update(JSON.stringify(canonicalRunDispatchValue({
    producer_kind: input.producerKind,
    scope_ref: input.scopeRef,
    thread_id: input.threadId,
    request_payload: input.requestPayload,
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

function canonicalRunDispatchRecord(value: Record<string, unknown>) {
  return canonicalRunDispatchValue(value) as Record<string, unknown>;
}

function canonicalRunDispatchValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalRunDispatchValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => compareUnicodeCodePoints(left, right))
        .map(([key, item]) => [key, canonicalRunDispatchValue(item)]),
    );
  }
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) {
      throw gatewayError("run_dispatch_request_invalid");
    }
    return value;
  }
  if (["string", "boolean"].includes(typeof value) || value === null) {
    return value;
  }
  throw gatewayError("run_dispatch_request_invalid");
}

function canonicalGatewayValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalGatewayValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => compareUnicodeCodePoints(left, right))
        .map(([key, item]) => [key, canonicalGatewayValue(item)]),
    );
  }
  if (["string", "number", "boolean"].includes(typeof value) || value === null) {
    return value;
  }
  throw gatewayError("run_dispatch_request_invalid");
}

function compareUnicodeCodePoints(left: string, right: string) {
  const leftPoints = Array.from(left, (item) => item.codePointAt(0) ?? 0);
  const rightPoints = Array.from(right, (item) => item.codePointAt(0) ?? 0);
  for (let index = 0; index < Math.min(leftPoints.length, rightPoints.length); index += 1) {
    if (leftPoints[index] !== rightPoints[index]) {
      return leftPoints[index] - rightPoints[index];
    }
  }
  return leftPoints.length - rightPoints.length;
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

function runDispatchProducerKind(value: unknown): RunDispatchProducerKind {
  if (value === "thread_message" || value === "clarification_resolution") {
    return value;
  }
  throw gatewayError("run_dispatch_producer_invalid");
}

function runDispatchCanStart(
  producerKind: unknown,
  runStatus: RunStatus,
) {
  const producer = runDispatchProducerKind(producerKind);
  return producer === "thread_message"
    ? runStatus === "queued"
    : runStatus === "waiting_for_clarification";
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
      producerKind: runDispatchProducerKind(row.producer_kind),
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


function isNonEmptyGatewayString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isTrimmedNonEmptyGatewayString(value: unknown): value is string {
  return isNonEmptyGatewayString(value) && value === value.trim();
}

const DELIVERY_STATUSES = new Set([
  "pending",
  "published",
  "retryable_failed",
  "permanently_failed",
]);

function requireCustomerPublication(value: unknown): CustomerPublication {
  try {
    return parseCustomerPublication(value);
  } catch {
    throw gatewayError("customer_publication_invalid");
  }
}

function requiredAcceptedGraphRecord(value: unknown): Record<string, unknown> {
  if (!isGatewayRecord(value)) throw gatewayError("workbench_accepted_graph_invalid");
  return value;
}

function publicationRefsFromRow(row: Record<string, unknown>): SafePublicationRefs {
  const publication = {
    authority_bundle_ref: row.authority_bundle_ref,
    authority_bundle_digest: row.authority_bundle_digest,
    authority_sealed_at: customerTimestamp(row.authority_sealed_at),
    publication_ref: row.publication_ref,
    publication_digest: row.publication_digest,
    published_at: customerTimestamp(row.published_at),
    projection_id: row.projection_id,
    projection_digest: row.projection_digest,
    outbox_ref: row.outbox_ref,
    delivery_status: row.delivery_status,
    delivery_attempted_at: customerTimestamp(row.delivery_attempted_at),
  };
  if (
    !isNonEmptyGatewayString(publication.authority_bundle_ref)
    || !isGatewayDigest(publication.authority_bundle_digest)
    || !isGatewayTimestamp(publication.authority_sealed_at)
    || !isNonEmptyGatewayString(publication.publication_ref)
    || !isGatewayDigest(publication.publication_digest)
    || !isGatewayTimestamp(publication.published_at)
    || !isNonEmptyGatewayString(publication.projection_id)
    || !isGatewayDigest(publication.projection_digest)
    || !isNonEmptyGatewayString(publication.outbox_ref)
    || !DELIVERY_STATUSES.has(String(publication.delivery_status ?? ""))
    || (
      publication.delivery_status === "pending"
        ? publication.delivery_attempted_at !== undefined
        : !isGatewayTimestamp(publication.delivery_attempted_at)
    )
  ) {
    throw gatewayError("publication_authority_invalid");
  }
  return publication as SafePublicationRefs;
}

function isGatewayTimestamp(value: unknown): value is string {
  return isNonEmptyGatewayString(value) && !Number.isNaN(Date.parse(value));
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

function normalizeInitialRequestIdentity(requestIdentity: string) {
  if (
    !isNonEmptyGatewayString(requestIdentity)
    || requestIdentity.trim().length > 256
    || /[\u0000-\u001f\u007f]/.test(requestIdentity)
  ) {
    throw gatewayError("run_dispatch_request_identity_invalid");
  }
  return requestIdentity.trim();
}


function memoryStore() {
  globalStore.__wajeConversationMemoryStore ??= {
    threads: new Map(),
    runs: new Map(),
    memoryProposals: new Map(),
    runDispatches: new Map(),
    auditEvents: [],
  };
  const store = globalStore.__wajeConversationMemoryStore;
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

async function publicationBoundaryRows(limit: number) {
  return pool().query(
    `
    SELECT
      r.run_id,
      r.thread_id,
      r.status AS run_status,
      bundle.authority_mode,
      bundle.bundle_ref AS authority_bundle_ref,
      bundle.created_at
    FROM waje_runtime.analysis_runs r
    JOIN waje_runtime.authority_bundles bundle
      ON bundle.run_attempt_id = r.run_attempt_id
    WHERE bundle.authority_mode = 'boundary_only'
    ORDER BY bundle.created_at DESC
    LIMIT $1
    `,
    [limit],
  );
}

async function deliveryFailureRows(limit: number) {
  return pool().query(
    `
    SELECT
      r.run_id,
      r.thread_id,
      r.status AS run_status,
      attempt.outbox_ref,
      attempt.status AS delivery_status,
      attempt.failure_code,
      attempt.attempted_at
    FROM waje_runtime.analysis_runs r
    JOIN waje_runtime.delivery_attempts attempt
      ON attempt.run_attempt_id = r.run_attempt_id
    WHERE attempt.status = 'permanently_failed'
    ORDER BY attempt.attempted_at DESC
    LIMIT $1
    `,
    [limit],
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

export function projectPlanResultForCustomer(value: unknown) {
  if (!isGatewayRecord(value)) return null;
  const authorityRefs = projectAuthorityRefs(value.authority_refs);
  if (
    value.schema_version !== "single-authority-phase02.v2"
    || value.status !== "planned"
    || !businessString(value.run_id)
    || !businessString(value.run_attempt_id)
    || !businessString(value.intent_revision_id)
    || !authorityRefs
  ) {
    return null;
  }
  const authorityBundle = requirePlanAuthorityBundle(value);
  const isPlanPatch = isNonEmptyGatewayString(
    authorityBundle.planRevision.supersedes_plan_revision_id,
  );
  if (
    isPlanPatch
      ? !isContentAddressedPlanPatchRef(value.plan_patch_ref)
      : authorityBundle.planRevision.supersedes_plan_revision_id !== null
        || value.plan_patch_ref !== null
  ) {
    return null;
  }
  const ledgerPosition = Number.isSafeInteger(value.decision_ledger_position)
    && Number(value.decision_ledger_position) >= 0
    ? Number(value.decision_ledger_position)
    : undefined;
  return compactGatewayRecord({
    schema_version: "single-authority-phase02.v2",
    run_id: businessString(value.run_id),
    run_attempt_id: businessString(value.run_attempt_id),
    status: "planned",
    intent_revision_id: businessString(value.intent_revision_id),
    plan_patch_ref: isPlanPatch ? businessString(value.plan_patch_ref) : null,
    decision_ledger_position: ledgerPosition,
    decision_refs: projectBusinessStringArray(value.decision_refs),
    authority_refs: authorityRefs,
    authority_context: projectAuthorityContext(authorityBundle.authorityContext),
    planner_proposal: projectPlannerProposal(authorityBundle.plannerProposal),
    proposal_admission_record: projectProposalAdmission(
      authorityBundle.proposalAdmissionRecord,
    ),
    plan_revision: projectPlanRevision(authorityBundle.planRevision),
  });
}

export function projectExecutionResultForCustomer(value: unknown) {
  if (!isExecutionProjectionAuthority(value)) {
    return null;
  }
  return projectExecutionAuthorityForCustomer({
    resultRef: String(value.authoritative_execution_result_ref),
    planRevisionId: String(value.plan_revision_id),
    executionSnapshotRef: String(value.execution_snapshot_ref),
    planRevision: value.plan_revision as Record<string, unknown>,
    explorationStopRecord: value.exploration_stop_record as Record<string, unknown>,
    bundles: value.capability_outcome_bundles as Record<string, unknown>[],
  });
}

const EXECUTION_PROJECTION_AUTHORITY_FIELDS = new Set([
  "authoritative_execution_result_ref",
  "schema_version",
  "status",
  "run_attempt_id",
  "intent_revision_id",
  "authority_context_ref",
  "plan_revision_id",
  "execution_snapshot_ref",
  "stop_ref",
  "transition_id",
  "plan_revision",
  "execution_snapshot",
  "exploration_stop_record",
  "capability_outcome_bundles",
  "durable_transition",
  "bundle_set_digest",
  "content_digest",
]);

const EXECUTION_OUTCOME_STATUSES = new Set([
  "succeeded",
  "unavailable",
  "integrity_failed",
  "technical_failed",
  "skipped",
  "superseded",
]);

const EXECUTION_EVIDENCE_STATES = new Set([
  "available",
  "unavailable",
  "integrity_failed",
  "technical_failed",
]);

const EXECUTION_EVIDENCE_KINDS = new Set([
  "boundary",
  "observed",
  "derived",
  "scenario",
  "statistical_association",
]);

const EXECUTION_STOP_REASONS = new Set([
  "plan_exhausted",
  "hard_budget_reached",
  "no_ready_tasks",
  "shared_authority_failure",
]);

function isExecutionProjectionAuthority(
  value: unknown,
): value is Record<string, unknown> {
  if (!isGatewayRecord(value)) return false;
  const keys = Object.keys(value);
  if (
    keys.length !== EXECUTION_PROJECTION_AUTHORITY_FIELDS.size
    || keys.some((key) => !EXECUTION_PROJECTION_AUTHORITY_FIELDS.has(key))
    || value.schema_version !== "single-authority-phase03.v1"
    || value.status !== "evidence_ready"
    || !isNonEmptyGatewayString(value.run_attempt_id)
    || !isNonEmptyGatewayString(value.intent_revision_id)
    || !isNonEmptyGatewayString(value.authority_context_ref)
    || !isNonEmptyGatewayString(value.plan_revision_id)
    || !isNonEmptyGatewayString(value.execution_snapshot_ref)
    || !isNonEmptyGatewayString(value.stop_ref)
    || !isNonEmptyGatewayString(value.transition_id)
    || !isGatewayDigest(value.bundle_set_digest)
    || !isGatewayDigest(value.content_digest)
    || value.authoritative_execution_result_ref
      !== `authoritative-execution-result:sha256:${value.content_digest}`
    || !isGatewayRecord(value.plan_revision)
    || !isGatewayRecord(value.execution_snapshot)
    || !isGatewayRecord(value.exploration_stop_record)
    || !isGatewayRecord(value.durable_transition)
    || !Array.isArray(value.capability_outcome_bundles)
  ) {
    return false;
  }
  const plan = value.plan_revision;
  const snapshot = value.execution_snapshot;
  const stop = value.exploration_stop_record;
  const transition = value.durable_transition;
  return plan.run_attempt_id === value.run_attempt_id
    && plan.intent_revision_id === value.intent_revision_id
    && plan.authority_context_ref === value.authority_context_ref
    && plan.plan_revision_id === value.plan_revision_id
    && Array.isArray(plan.capability_tasks)
    && plan.capability_tasks.every(isGatewayRecord)
    && Array.isArray(plan.claim_obligations)
    && plan.claim_obligations.every(isGatewayRecord)
    && snapshot.execution_snapshot_ref === value.execution_snapshot_ref
    && snapshot.run_attempt_id === value.run_attempt_id
    && snapshot.authority_context_ref === value.authority_context_ref
    && snapshot.plan_revision_id === value.plan_revision_id
    && snapshot.stop_ref === value.stop_ref
    && stop.stop_ref === value.stop_ref
    && stop.run_attempt_id === value.run_attempt_id
    && stop.plan_revision_id === value.plan_revision_id
    && Array.isArray(stop.evaluated_outcome_refs)
    && transition.transition_id === value.transition_id
    && transition.run_attempt_id === value.run_attempt_id
    && transition.intent_revision_id === value.intent_revision_id
    && transition.node_name === "execute_capability_dag"
    && transition.status === "succeeded"
    && transition.acceptance_state === "accepted"
    && transition.next_transition === "phase03_evidence_bound"
    && isGatewayDigest(transition.input_digest)
    && isGatewayDigest(transition.output_digest)
    && value.capability_outcome_bundles.every((bundle) => (
      isGatewayRecord(bundle)
      && Object.keys(bundle).length === 4
      && ["attempt", "outcome", "evidence_entries", "failure_records"]
        .every((field) => Object.prototype.hasOwnProperty.call(bundle, field))
      && isGatewayRecord(bundle.attempt)
      && isGatewayRecord(bundle.outcome)
      && Array.isArray(bundle.evidence_entries)
      && bundle.evidence_entries.every(isGatewayRecord)
      && Array.isArray(bundle.failure_records)
      && bundle.failure_records.every(isGatewayRecord)
    ))
    && hasExecutionProjectionClosure({
      runAttemptId: String(value.run_attempt_id),
      intentRevisionId: String(value.intent_revision_id),
      authorityContextRef: String(value.authority_context_ref),
      planRevisionId: String(value.plan_revision_id),
      executionSnapshotRef: String(value.execution_snapshot_ref),
      stopRef: String(value.stop_ref),
      planRevision: plan,
      executionSnapshot: snapshot,
      explorationStopRecord: stop,
      bundles: value.capability_outcome_bundles as Record<string, unknown>[],
    });
}

type ExecutionProjectionAuthority = {
  resultRef: string;
  planRevisionId: string;
  executionSnapshotRef: string;
  planRevision: Record<string, unknown>;
  explorationStopRecord: Record<string, unknown>;
  bundles: Record<string, unknown>[];
};

type ExecutionProjectionClosure = {
  runAttemptId: string;
  intentRevisionId: string;
  authorityContextRef: string;
  planRevisionId: string;
  executionSnapshotRef: string;
  stopRef: string;
  planRevision: Record<string, unknown>;
  executionSnapshot: Record<string, unknown>;
  explorationStopRecord: Record<string, unknown>;
  bundles: Record<string, unknown>[];
};

function hasExecutionProjectionClosure(
  authority: ExecutionProjectionClosure,
) {
  const plan = authority.planRevision;
  const snapshot = authority.executionSnapshot;
  const stop = authority.explorationStopRecord;
  if (
    plan.run_attempt_id !== authority.runAttemptId
    || plan.intent_revision_id !== authority.intentRevisionId
    || plan.authority_context_ref !== authority.authorityContextRef
    || plan.plan_revision_id !== authority.planRevisionId
    || !Array.isArray(plan.capability_tasks)
    || !Array.isArray(plan.claim_obligations)
    || snapshot.execution_snapshot_ref !== authority.executionSnapshotRef
    || snapshot.run_attempt_id !== authority.runAttemptId
    || snapshot.authority_context_ref !== authority.authorityContextRef
    || snapshot.plan_revision_id !== authority.planRevisionId
    || snapshot.stop_ref !== authority.stopRef
    || !isUniqueGatewayStringArray(snapshot.outcome_refs)
    || !isUniqueGatewayStringArray(snapshot.evidence_entry_refs)
    || !isUniqueGatewayStringArray(snapshot.failure_refs)
    || stop.stop_ref !== authority.stopRef
    || stop.run_attempt_id !== authority.runAttemptId
    || stop.plan_revision_id !== authority.planRevisionId
    || !isUniqueGatewayStringArray(stop.evaluated_outcome_refs)
    || !EXECUTION_STOP_REASONS.has(String(stop.reason ?? ""))
    || !Number.isSafeInteger(stop.used_budget_units)
    || Number(stop.used_budget_units) < 0
    || !(
      stop.hard_budget_limit === null
      || (
        Number.isSafeInteger(stop.hard_budget_limit)
        && Number(stop.hard_budget_limit) >= 0
      )
    )
  ) {
    return false;
  }

  const obligationIds = new Set<string>();
  for (const obligation of plan.claim_obligations) {
    const evidenceRequirement = isGatewayRecord(obligation)
      ? obligation.evidence_requirement
      : null;
    if (
      !isGatewayRecord(obligation)
      || !isNonEmptyGatewayString(obligation.obligation_id)
      || obligationIds.has(String(obligation.obligation_id))
      || !isNonEmptyGatewayString(obligation.claim_kind)
      || !isNonEmptyGatewayString(obligation.role)
      || !isGatewayRecord(evidenceRequirement)
      || evidenceRequirement.operator !== "any_of"
      || !isUniqueGatewayStringArray(evidenceRequirement.evidence_kinds)
      || evidenceRequirement.evidence_kinds.length === 0
    ) {
      return false;
    }
    obligationIds.add(String(obligation.obligation_id));
  }

  const tasks = new Map<string, Record<string, unknown>>();
  for (const task of plan.capability_tasks) {
    if (
      !isGatewayRecord(task)
      || !isNonEmptyGatewayString(task.task_id)
      || tasks.has(String(task.task_id))
      || task.plan_revision_id !== authority.planRevisionId
      || task.authority_context_ref !== authority.authorityContextRef
      || !isNonEmptyGatewayString(task.capability_id)
      || !Array.isArray(task.obligation_edges)
      || !isUniqueGatewayStringArray(task.supports_obligation_ids)
    ) {
      return false;
    }
    const edgeIds = new Set<string>();
    for (const edge of task.obligation_edges) {
      if (
        !isGatewayRecord(edge)
        || !isNonEmptyGatewayString(edge.obligation_id)
        || edgeIds.has(String(edge.obligation_id))
        || !obligationIds.has(String(edge.obligation_id))
        || typeof edge.required !== "boolean"
      ) {
        return false;
      }
      edgeIds.add(String(edge.obligation_id));
    }
    if (
      task.supports_obligation_ids.some(
        (obligationId) => !edgeIds.has(obligationId),
      )
    ) {
      return false;
    }
    tasks.set(String(task.task_id), task);
  }

  const outcomeRefs = new Set<string>();
  const attemptedTaskIds = new Set<string>();
  const attemptIds = new Set<string>();
  const evidenceEntryRefs = new Set<string>();
  const failureRefs = new Set<string>();
  let usedBudgetUnits = 0;
  for (const bundle of authority.bundles) {
    if (
      !isGatewayRecord(bundle)
      || Object.keys(bundle).length !== 4
      || !["attempt", "outcome", "evidence_entries", "failure_records"]
        .every((field) => Object.prototype.hasOwnProperty.call(bundle, field))
      || !isGatewayRecord(bundle.attempt)
      || !isGatewayRecord(bundle.outcome)
      || !Array.isArray(bundle.evidence_entries)
      || !Array.isArray(bundle.failure_records)
    ) {
      return false;
    }
    const attempt = bundle.attempt;
    const outcome = bundle.outcome;
    const taskId = isNonEmptyGatewayString(attempt.task_id)
      ? attempt.task_id
      : undefined;
    const attemptId = isNonEmptyGatewayString(attempt.attempt_id)
      ? attempt.attempt_id
      : undefined;
    const outcomeRef = isNonEmptyGatewayString(outcome.outcome_ref)
      ? outcome.outcome_ref
      : undefined;
    const task = taskId ? tasks.get(taskId) : undefined;
    if (
      !taskId
      || !task
      || !attemptId
      || !outcomeRef
      || attemptedTaskIds.has(taskId)
      || attemptIds.has(attemptId)
      || outcomeRefs.has(outcomeRef)
      || attempt.run_attempt_id !== authority.runAttemptId
      || attempt.intent_revision_id !== authority.intentRevisionId
      || attempt.plan_revision_id !== authority.planRevisionId
      || !Number.isSafeInteger(attempt.execution_attempt)
      || Number(attempt.execution_attempt) < 1
      || outcome.run_attempt_id !== authority.runAttemptId
      || outcome.plan_revision_id !== authority.planRevisionId
      || outcome.task_id !== taskId
      || outcome.attempt_id !== attemptId
      || outcome.input_digest !== attempt.input_digest
      || !EXECUTION_OUTCOME_STATUSES.has(String(outcome.status ?? ""))
      || !isUniqueGatewayStringArray(outcome.evidence_refs)
      || !isUniqueGatewayStringArray(outcome.affected_obligation_ids)
      || !isUniqueGatewayStringArray(outcome.limitation_refs)
      || !Number.isSafeInteger(outcome.budget_units)
      || Number(outcome.budget_units) < 0
      || !(
        outcome.failure_ref === null
        || isNonEmptyGatewayString(outcome.failure_ref)
      )
      || (
        outcome.status === "succeeded"
        && outcome.evidence_refs.length === 0
      )
      || (
        (outcome.status === "integrity_failed"
          || outcome.status === "technical_failed")
        && outcome.failure_ref === null
      )
    ) {
      return false;
    }
    const supported = new Set(task.supports_obligation_ids as string[]);
    if (
      outcome.affected_obligation_ids.some(
        (obligationId) => !supported.has(obligationId),
      )
    ) {
      return false;
    }
    usedBudgetUnits += Number(outcome.budget_units);
    attemptedTaskIds.add(taskId);
    attemptIds.add(attemptId);
    outcomeRefs.add(outcomeRef);

    const bundleEvidenceRefs = new Set<string>();
    for (const entry of bundle.evidence_entries) {
      if (
        !isGatewayRecord(entry)
        || !isNonEmptyGatewayString(entry.entry_ref)
        || !isNonEmptyGatewayString(entry.evidence_ref)
        || evidenceEntryRefs.has(String(entry.entry_ref))
        || bundleEvidenceRefs.has(String(entry.evidence_ref))
        || entry.run_attempt_id !== authority.runAttemptId
        || entry.authority_context_ref !== authority.authorityContextRef
        || entry.plan_revision_id !== authority.planRevisionId
        || entry.task_id !== taskId
        || entry.outcome_ref !== outcomeRef
        || !EXECUTION_EVIDENCE_STATES.has(String(entry.execution_state ?? ""))
        || !EXECUTION_EVIDENCE_KINDS.has(String(entry.evidence_kind ?? ""))
        || !isNonEmptyGatewayString(entry.data_contract_state)
        || !isNonEmptyGatewayString(entry.evidence_strength)
        || !isNonEmptyGatewayString(entry.maximum_claim_strength)
        || !isNonEmptyGatewayString(entry.scope)
        || !isUniqueGatewayStringArray(entry.supported_claim_kinds)
        || !isUniqueGatewayStringArray(entry.window_refs)
        || !isUniqueGatewayStringArray(entry.result_refs)
        || !isUniqueGatewayStringArray(entry.completeness_report_refs)
        || !isUniqueGatewayStringArray(entry.dimension_path)
        || typeof entry.hierarchy_qualified !== "boolean"
        || !isUniqueGatewayStringArray(entry.limitation_refs)
      ) {
        return false;
      }
      evidenceEntryRefs.add(String(entry.entry_ref));
      bundleEvidenceRefs.add(String(entry.evidence_ref));
    }
    if (!sameGatewayStringSet(outcome.evidence_refs, bundleEvidenceRefs)) {
      return false;
    }

    const bundleFailureRefs = new Set<string>();
    for (const failure of bundle.failure_records) {
      if (
        !isGatewayRecord(failure)
        || !isNonEmptyGatewayString(failure.failure_ref)
        || failureRefs.has(String(failure.failure_ref))
        || bundleFailureRefs.has(String(failure.failure_ref))
        || failure.run_attempt_id !== authority.runAttemptId
        || failure.plan_revision_id !== authority.planRevisionId
        || failure.task_id !== taskId
        || failure.attempt_id !== attemptId
        || !isNonEmptyGatewayString(failure.scope)
        || !isNonEmptyGatewayString(failure.integrity_level)
        || !isNonEmptyGatewayString(failure.retryability)
        || typeof failure.user_actionable !== "boolean"
        || !isNonEmptyGatewayString(failure.business_boundary)
      ) {
        return false;
      }
      failureRefs.add(String(failure.failure_ref));
      bundleFailureRefs.add(String(failure.failure_ref));
    }
    const expectedFailures = outcome.failure_ref === null
      ? new Set<string>()
      : new Set([String(outcome.failure_ref)]);
    if (!sameGatewayStringSet([...bundleFailureRefs], expectedFailures)) {
      return false;
    }
  }

  return usedBudgetUnits === Number(stop.used_budget_units)
    && (
      stop.reason !== "plan_exhausted"
      || attemptedTaskIds.size === tasks.size
    )
    && (
      stop.reason !== "hard_budget_reached"
      || (
        stop.hard_budget_limit !== null
        && usedBudgetUnits >= Number(stop.hard_budget_limit)
      )
    )
    && sameGatewayStringSet(snapshot.outcome_refs, outcomeRefs)
    && sameGatewayStringSet(stop.evaluated_outcome_refs, outcomeRefs)
    && sameGatewayStringSet(snapshot.evidence_entry_refs, evidenceEntryRefs)
    && sameGatewayStringSet(snapshot.failure_refs, failureRefs);
}

function isUniqueGatewayStringArray(value: unknown): value is string[] {
  return Array.isArray(value)
    && value.every(isNonEmptyGatewayString)
    && new Set(value).size === value.length;
}

function sameGatewayStringSet(
  values: string[],
  expected: ReadonlySet<string>,
) {
  return values.length === expected.size
    && values.every((value) => expected.has(value));
}

function projectExecutionAuthorityForCustomer(
  authority: ExecutionProjectionAuthority,
) {
  const tasks = (authority.planRevision.capability_tasks as unknown[])
    .filter(isGatewayRecord)
    .map((task) => ({
      task_id: String(task.task_id),
      capability_id: String(task.capability_id),
      obligations: (task.obligation_edges as unknown[])
        .filter(isGatewayRecord)
        .map((edge) => ({
          obligation_id: String(edge.obligation_id),
          required: edge.required === true,
        })),
    }))
    .sort((left, right) => left.task_id.localeCompare(right.task_id));
  const obligations = (authority.planRevision.claim_obligations as unknown[])
    .filter(isGatewayRecord)
    .map((obligation) => {
      const evidenceRequirement = obligation.evidence_requirement as Record<
        string,
        unknown
      >;
      return {
        obligation_id: String(obligation.obligation_id),
        claim_kind: String(obligation.claim_kind),
        role: String(obligation.role),
        evidence_requirement: {
          operator: String(evidenceRequirement.operator),
          evidence_kinds: [
            ...evidenceRequirement.evidence_kinds as string[],
          ],
        },
      };
    })
    .sort((left, right) => (
      left.obligation_id.localeCompare(right.obligation_id)
    ));
  const bundles = authority.bundles;
  const outcomes = bundles
    .map((bundle) => bundle.outcome)
    .filter(isGatewayRecord)
    .map((outcome) => ({
      outcome_ref: String(outcome.outcome_ref),
      task_id: String(outcome.task_id),
      status: String(outcome.status),
      evidence_refs: [...outcome.evidence_refs as string[]],
      affected_obligation_ids: [
        ...outcome.affected_obligation_ids as string[],
      ],
      limitation_refs: [...outcome.limitation_refs as string[]],
    }))
    .sort((left, right) => left.outcome_ref.localeCompare(right.outcome_ref));
  const evidence = bundles
    .flatMap((bundle) => bundle.evidence_entries as unknown[])
    .filter(isGatewayRecord)
    .map((entry) => ({
      evidence_entry_ref: String(entry.entry_ref),
      evidence_ref: String(entry.evidence_ref),
      task_id: String(entry.task_id),
      outcome_ref: String(entry.outcome_ref),
      status: String(entry.execution_state),
      evidence_kind: String(entry.evidence_kind),
      data_contract_state: String(entry.data_contract_state),
      supported_claim_kinds: [...entry.supported_claim_kinds as string[]],
      evidence_strength: String(entry.evidence_strength),
      maximum_claim_strength: String(entry.maximum_claim_strength),
      scope: String(entry.scope),
      window_refs: [...entry.window_refs as string[]],
      result_refs: [...entry.result_refs as string[]],
      completeness_report_refs: [
        ...entry.completeness_report_refs as string[],
      ],
      dimension_path: [...entry.dimension_path as string[]],
      hierarchy_qualified: entry.hierarchy_qualified === true,
      limitation_refs: [...entry.limitation_refs as string[]],
    }))
    .sort((left, right) => (
      left.evidence_entry_ref.localeCompare(right.evidence_entry_ref)
    ));
  const failures = bundles
    .flatMap((bundle) => bundle.failure_records as unknown[])
    .filter(isGatewayRecord)
    .map((failure) => ({
      failure_ref: String(failure.failure_ref),
      task_id: String(failure.task_id),
      scope: String(failure.scope),
      integrity_level: String(failure.integrity_level),
      retryability: String(failure.retryability),
      user_actionable: failure.user_actionable === true,
      business_boundary: String(failure.business_boundary),
    }))
    .sort((left, right) => left.failure_ref.localeCompare(right.failure_ref));
  const limitations = [...new Set([
    ...outcomes.flatMap((outcome) => outcome.limitation_refs),
    ...evidence.flatMap((entry) => entry.limitation_refs),
  ])].sort();
  const stop = authority.explorationStopRecord;
  return {
    schema_version: "single-authority-phase03.v1",
    status: "evidence_ready",
    result_ref: authority.resultRef,
    plan_revision_id: authority.planRevisionId,
    execution_snapshot_ref: authority.executionSnapshotRef,
    tasks,
    outcomes,
    obligations,
    evidence,
    failures,
    limitations,
    stop: {
      stop_ref: String(stop.stop_ref),
      reason: String(stop.reason),
      evaluated_outcome_refs: [...stop.evaluated_outcome_refs as string[]],
      used_budget_units: Number(stop.used_budget_units),
      hard_budget_limit: stop.hard_budget_limit === null
        ? null
        : Number(stop.hard_budget_limit),
    },
  };
}

function requirePlanAuthorityBundle(value: Record<string, unknown>) {
  if (
    !isGatewayRecord(value.authority_context)
    || !isGatewayRecord(value.planner_proposal)
    || !isGatewayRecord(value.proposal_admission_record)
    || !isGatewayRecord(value.plan_revision)
  ) {
    throw gatewayError("planned_result_authority_bundle_incomplete");
  }
  return {
    authorityContext: value.authority_context,
    plannerProposal: value.planner_proposal,
    proposalAdmissionRecord: value.proposal_admission_record,
    planRevision: value.plan_revision,
  };
}

function projectAuthorityRefs(value: unknown) {
  if (!isGatewayRecord(value)) return null;
  const projected = compactGatewayRecord({
    intent_revision_id: businessString(value.intent_revision_id),
    authority_context_ref: businessString(value.authority_context_ref),
    planner_proposal_id: businessString(value.planner_proposal_id),
    proposal_admission_id: businessString(value.proposal_admission_id),
    plan_revision_id: businessString(value.plan_revision_id),
    accepted_transition_id: businessString(value.accepted_transition_id),
  });
  return Object.keys(projected).length === 6 ? projected : null;
}

const CLAIM_COVERAGE_REF_FIELDS = [
  "schema_version",
  "source_plan_revision_id",
  "source_execution_result_ref",
  "claim_coverage_checkpoint_ref",
  "claim_coverage_checkpoint_digest",
  "claim_coverage_evaluation_ref",
  "plan_expansion_decision_ref",
  "decision",
  "plan_patch_ref",
  "accepted_transition_id",
] as const;

export function projectClaimCoverageRefsForCustomer(value: unknown) {
  if (
    !isGatewayRecord(value)
    || Object.keys(value).length !== CLAIM_COVERAGE_REF_FIELDS.length
    || !CLAIM_COVERAGE_REF_FIELDS.every((field) => (
      Object.prototype.hasOwnProperty.call(value, field)
    ))
    || value.schema_version !== "claim-coverage-checkpoint.v1"
    || !["seal", "patch"].includes(String(value.decision ?? ""))
    || !isGatewayDigest(value.claim_coverage_checkpoint_digest)
    || value.claim_coverage_checkpoint_ref
      !== `claim-coverage-checkpoint:sha256:${value.claim_coverage_checkpoint_digest}`
    || ![
      "source_plan_revision_id",
      "source_execution_result_ref",
      "claim_coverage_evaluation_ref",
      "plan_expansion_decision_ref",
      "accepted_transition_id",
    ].every((field) => isNonEmptyGatewayString(value[field]))
    || (
      value.decision === "seal"
        ? value.plan_patch_ref !== null
        : !isContentAddressedPlanPatchRef(value.plan_patch_ref)
    )
  ) {
    return null;
  }
  return {
    schema_version: value.schema_version as string,
    source_plan_revision_id: value.source_plan_revision_id as string,
    source_execution_result_ref: value.source_execution_result_ref as string,
    claim_coverage_checkpoint_ref: value.claim_coverage_checkpoint_ref as string,
    claim_coverage_checkpoint_digest: (
      value.claim_coverage_checkpoint_digest as string
    ),
    claim_coverage_evaluation_ref: value.claim_coverage_evaluation_ref as string,
    plan_expansion_decision_ref: value.plan_expansion_decision_ref as string,
    decision: value.decision as "seal" | "patch",
    plan_patch_ref: value.plan_patch_ref as string | null,
    accepted_transition_id: value.accepted_transition_id as string,
  };
}

function projectAuthorityContext(value: unknown) {
  if (!isGatewayRecord(value)) return undefined;
  return compactGatewayRecord({
    authority_context_ref: businessString(value.authority_context_ref),
    run_attempt_id: businessString(value.run_attempt_id),
    actual_as_of: businessString(value.actual_as_of),
    release_refs: projectBusinessStringArray(value.release_refs),
    snapshot_refs: projectBusinessStringArray(value.snapshot_refs),
    dataset_coverage: projectGatewayRecordArray(
      value.dataset_coverage,
      (item) => compactGatewayRecord({
        dataset_id: businessString(item.dataset_id),
        availability: businessString(item.availability),
        release_ref: businessString(item.release_ref) ?? null,
        snapshot_refs: projectBusinessStringArray(item.snapshot_refs),
        limitation_ref: businessString(item.limitation_ref) ?? null,
      }),
    ),
    contract_versions: projectStringRecord(value.contract_versions),
    content_digest: businessString(value.content_digest),
  });
}

function projectPlannerProposal(value: unknown) {
  if (!isGatewayRecord(value)) return undefined;
  return compactGatewayRecord({
    planner_proposal_id: businessString(value.planner_proposal_id),
    run_attempt_id: businessString(value.run_attempt_id),
    intent_revision_id: businessString(value.intent_revision_id),
    decision_refs: projectBusinessStringArray(value.decision_refs),
    authority_context_ref: businessString(value.authority_context_ref),
    issue_tree: projectGatewayRecordArray(
      value.issue_tree,
      (item) => compactGatewayRecord({
        issue_id: businessString(item.issue_id),
        parent_issue_id: businessString(item.parent_issue_id) ?? null,
        question: businessString(item.question),
        target_claim_kind: businessString(item.target_claim_kind),
      }),
    ),
    auxiliary_axes: projectGatewayRecordArray(
      value.auxiliary_axes,
      (item) => compactGatewayRecord({
        proposal_item_id: businessString(item.proposal_item_id),
        axis_id: businessString(item.axis_id),
        rationale: businessString(item.rationale),
        supports_claim_kinds: projectBusinessStringArray(
          item.supports_claim_kinds,
        ),
      }),
    ),
    hypotheses: projectGatewayRecordArray(
      value.hypotheses,
      (item) => compactGatewayRecord({
        proposal_item_id: businessString(item.proposal_item_id),
        statement: businessString(item.statement),
        target_claim_kind: businessString(item.target_claim_kind),
        requested_axis_ids: projectBusinessStringArray(item.requested_axis_ids),
        assumption_refs: projectBusinessStringArray(item.assumption_refs),
      }),
    ),
    priority_proposals: projectGatewayRecordArray(
      value.priority_proposals,
      (item) => compactGatewayRecord({
        proposal_item_id: businessString(item.proposal_item_id),
        target_ref: businessString(item.target_ref),
        rationale: businessString(item.rationale),
      }),
    ),
    schema_version: businessString(value.schema_version),
    prompt_version: businessString(value.prompt_version),
    model_version: businessString(value.model_version),
    content_digest: businessString(value.content_digest),
  });
}

function projectProposalAdmission(value: unknown) {
  if (!isGatewayRecord(value)) return undefined;
  return compactGatewayRecord({
    proposal_admission_id: businessString(value.proposal_admission_id),
    planner_proposal_ref: businessString(value.planner_proposal_ref),
    intent_revision_id: businessString(value.intent_revision_id),
    decision_refs: projectBusinessStringArray(value.decision_refs),
    authority_context_ref: businessString(value.authority_context_ref),
    admission_entries: projectGatewayRecordArray(
      value.admission_entries,
      (item) => compactGatewayRecord({
        proposal_item_ref: businessString(item.proposal_item_ref),
        item_kind: businessString(item.item_kind),
        status: businessString(item.status),
        reason_code: businessString(item.reason_code),
        contract_refs: projectBusinessStringArray(item.contract_refs),
        normalized_execution_ref:
          businessString(item.normalized_execution_ref) ?? null,
      }),
    ),
    compiler_version: businessString(value.compiler_version),
    contract_versions: projectStringRecord(value.contract_versions),
    content_digest: businessString(value.content_digest),
  });
}

function projectPlanRevision(value: unknown) {
  if (!isGatewayRecord(value)) return undefined;
  return compactGatewayRecord({
    plan_revision_id: businessString(value.plan_revision_id),
    run_attempt_id: businessString(value.run_attempt_id),
    supersedes_plan_revision_id:
      businessString(value.supersedes_plan_revision_id) ?? null,
    intent_revision_id: businessString(value.intent_revision_id),
    decision_refs: projectBusinessStringArray(value.decision_refs),
    authority_context_ref: businessString(value.authority_context_ref),
    planner_proposal_ref: businessString(value.planner_proposal_ref),
    proposal_admission_ref: businessString(value.proposal_admission_ref),
    resolved_window_refs: projectBusinessStringArray(value.resolved_window_refs),
    context_window_specs: projectGatewayRecordArray(
      value.context_window_specs,
      projectPlanContextWindowSpec,
    ),
    claim_obligations: projectGatewayRecordArray(
      value.claim_obligations,
      projectClaimObligation,
    ),
    analysis_axes: projectGatewayRecordArray(
      value.analysis_axes,
      projectAnalysisAxis,
    ),
    capability_tasks: projectGatewayRecordArray(
      value.capability_tasks,
      projectCapabilityTask,
    ),
    assumption_refs: projectBusinessStringArray(value.assumption_refs),
    budget_policy_ref: businessString(value.budget_policy_ref),
    contract_versions: projectStringRecord(value.contract_versions),
    content_digest: businessString(value.content_digest),
  });
}

function projectPlanContextWindowSpec(item: Record<string, unknown>) {
  return compactGatewayRecord({
    capability_id: businessString(item.capability_id),
    relation: businessString(item.relation),
    unit: businessString(item.unit),
    count:
      typeof item.count === "number" && Number.isInteger(item.count)
        ? item.count
        : undefined,
  });
}

function projectClaimObligation(item: Record<string, unknown>) {
  const subject = isGatewayRecord(item.subject) ? item.subject : {};
  const evidenceRequirement = isGatewayRecord(item.evidence_requirement)
    ? item.evidence_requirement
    : {};
  const successPolicy = isGatewayRecord(item.success_policy)
    ? item.success_policy
    : {};
  return compactGatewayRecord({
    obligation_id: businessString(item.obligation_id),
    claim_kind: businessString(item.claim_kind),
    role: businessString(item.role),
    subject: compactGatewayRecord({
      target_metric_ref: businessString(subject.target_metric_ref),
      target_metric_refs: projectBusinessStringArray(subject.target_metric_refs),
      outcome_refs: projectBusinessStringArray(subject.outcome_refs),
      goal_refs: projectBusinessStringArray(subject.goal_refs),
      planner_proposal_ref: businessString(subject.planner_proposal_ref),
      proposal_item_ref: businessString(subject.proposal_item_ref),
    }),
    evidence_requirement: compactGatewayRecord({
      operator: businessString(evidenceRequirement.operator),
      evidence_kinds: projectBusinessStringArray(
        evidenceRequirement.evidence_kinds,
      ),
    }),
    success_policy: compactGatewayRecord({
      policy: businessString(successPolicy.policy),
      outcome_refs: projectBusinessStringArray(successPolicy.outcome_refs),
      requested_axis_ids: projectBusinessStringArray(
        successPolicy.requested_axis_ids,
      ),
    }),
    content_digest: businessString(item.content_digest),
  });
}

function projectAnalysisAxis(item: Record<string, unknown>) {
  return compactGatewayRecord({
    analysis_axis_ref: businessString(item.analysis_axis_ref),
    axis_id: businessString(item.axis_id),
    role: businessString(item.role),
    axis_kind: businessString(item.axis_kind),
    target_metric_refs: projectBusinessStringArray(item.target_metric_refs),
    metric_refs: projectBusinessStringArray(item.metric_refs),
    dimension_refs: projectBusinessStringArray(item.dimension_refs),
    context_source_refs: projectBusinessStringArray(item.context_source_refs),
    capability_refs: projectBusinessStringArray(item.capability_refs),
    reconciliation_group: businessString(item.reconciliation_group),
    selection_policy: businessString(item.selection_policy),
    source_refs: projectBusinessStringArray(item.source_refs),
    goal_refs: projectBusinessStringArray(item.goal_refs),
    supports_obligation_ids: projectBusinessStringArray(
      item.supports_obligation_ids,
    ),
    proposal_refs: projectBusinessStringArray(item.proposal_refs),
    content_digest: businessString(item.content_digest),
  });
}

function projectCapabilityTask(item: Record<string, unknown>) {
  const policy = isGatewayRecord(item.execution_policy)
    ? item.execution_policy
    : {};
  const degradation = isGatewayRecord(policy.degradation_policy)
    ? policy.degradation_policy
    : {};
  return compactGatewayRecord({
    task_id: businessString(item.task_id),
    task_key: businessString(item.task_key),
    plan_revision_id: businessString(item.plan_revision_id),
    authority_context_ref: businessString(item.authority_context_ref),
    capability_id: businessString(item.capability_id),
    normalized_input_refs: projectBusinessStringArray(item.normalized_input_refs),
    dependency_task_ids: projectBusinessStringArray(item.dependency_task_ids),
    obligation_edges: projectGatewayRecordArray(
      item.obligation_edges,
      (edge) => compactGatewayRecord({
        obligation_id: businessString(edge.obligation_id),
        required: typeof edge.required === "boolean" ? edge.required : undefined,
      }),
    ),
    supports_obligation_ids: projectBusinessStringArray(
      item.supports_obligation_ids,
    ),
    execution_policy: compactGatewayRecord({
      integrity_failure: businessString(policy.integrity_failure),
      degradation_policy: compactGatewayRecord({
        missing_required_input: businessString(degradation.missing_required_input),
        missing_optional_input: businessString(degradation.missing_optional_input),
        incomplete_input: businessString(degradation.incomplete_input),
      }),
      input_states: projectGatewayRecordArray(
        policy.input_states,
        (state) => compactGatewayRecord({
          input_ref: businessString(state.input_ref),
          availability: businessString(state.availability),
          limitation_ref: businessString(state.limitation_ref) ?? null,
        }),
      ),
    }),
    content_digest: businessString(item.content_digest),
  });
}

function projectGatewayRecordArray(
  value: unknown,
  projector: (item: Record<string, unknown>) => Record<string, unknown>,
) {
  return Array.isArray(value)
    ? value.flatMap((item) => isGatewayRecord(item) ? [projector(item)] : [])
    : undefined;
}

function projectBusinessStringArray(value: unknown) {
  return Array.isArray(value)
    ? value.flatMap((item) => {
        const projected = businessString(item);
        return projected === undefined ? [] : [projected];
      })
    : undefined;
}

function projectStringRecord(value: unknown) {
  if (!isGatewayRecord(value)) return undefined;
  return Object.fromEntries(
    Object.entries(value).flatMap(([key, item]) => {
      if (PLAN_RESULT_PRIVATE_FIELDS.has(key)) return [];
      const projected = businessString(item);
      return projected === undefined ? [] : [[key, projected]];
    }),
  );
}

const PLAN_RESULT_PRIVATE_FIELDS = new Set([
  "raw_provider_response_ref",
  "raw_provider_response",
  "provider_response",
  "llm_calls",
  "durable_checkpoint",
  "checkpoint_events",
]);

function compactGatewayRecord(value: Record<string, unknown>) {
  return Object.fromEntries(
    Object.entries(value).filter(([, item]) => item !== undefined),
  );
}

export function projectAgentCoreForCustomer(agentCore: Record<string, unknown>) {
  const resumed = isGatewayRecord(agentCore.result)
    ? agentCore.result
    : {};
  const status = String(agentCore.status ?? "");
  const innerStatus = businessString(resumed.status);
  if (!CUSTOMER_AGENT_CORE_STATUSES.has(status)) {
    return filterAgentCoreFailure(agentCore, resumed, true);
  }
  if (agentCoreStatusMismatch(status, innerStatus)) {
    return filterAgentCoreFailure(agentCore, resumed, true);
  }
  const visibleResult = {
    run_id: resumed.run_id,
    turn_id: resumed.turn_id,
    topic_id: resumed.topic_id,
    status,
  };

  if (
    status === "completed"
    || status === "authority_sealed"
    || status === "narrative_ready"
  ) {
    const postExecution = projectPostExecutionStateForCustomer(resumed);
    if (!postExecution) {
      throw gatewayError("post_execution_state_invalid");
    }
    return {
      status,
      result: {
        ...visibleResult,
        ...postExecution,
      },
    };
  }
  if (status === "interaction_completed") {
    const interaction = projectInteractionResultForCustomer(
      resumed.interaction_result,
    );
    const topicRelation = businessString(resumed.topic_relation);
    if (
      !interaction
      || resumed.intent !== interaction.intent
      || !topicRelation
      || (topicRelation === "ask_topic_choice")
        !== (interaction.schema_version === "typed-topic-choice.v1")
      || (
        interaction.schema_version === "typed-interaction.v1"
        && !INTERACTION_TOPIC_RELATIONS.has(topicRelation)
      )
      || (
        (interaction.intent === "analysis_cancellation")
        !== (topicRelation === "analysis_cancellation")
      )
    ) {
      throw gatewayError("interaction_result_invalid");
    }
    return {
      status,
      result: {
        ...visibleResult,
        intent: businessString(resumed.intent),
        topic_relation: businessString(resumed.topic_relation),
        interaction_result: interaction,
      },
    };
  }
  if (status === "waiting_for_clarification") {
    const clarification = filterBusinessClarification(resumed.clarification);
    if (!clarification) throw gatewayError("clarification_payload_invalid");
    return {
      status,
      result: {
        ...visibleResult,
        clarification,
      },
    };
  }
  if (status === "planned") {
    const planResult = projectPlanResultForCustomer(resumed.plan_result);
    if (!planResult) {
      throw gatewayError("planned_result_authority_refs_invalid");
    }
    return {
      status,
      result: {
        ...visibleResult,
        plan_result: planResult,
      },
    };
  }
  if (status === "evidence_ready") {
    const executionResult = projectExecutionResultForCustomer(
      resumed.execution_result,
    );
    const claimCoverage = projectClaimCoverageRefsForCustomer(
      resumed.claim_coverage,
    );
    if (
      !executionResult
      || !claimCoverage
      || claimCoverage.decision !== "seal"
      || claimCoverage.source_plan_revision_id
        !== executionResult.plan_revision_id
      || claimCoverage.source_execution_result_ref
        !== executionResult.result_ref
    ) {
      throw gatewayError("authoritative_execution_result_invalid");
    }
    return {
      status,
      result: {
        ...visibleResult,
        execution_result: executionResult,
        claim_coverage: claimCoverage,
      },
    };
  }
  if (
    status === "material_revision_required"
    || status === "run_cancelled"
    || status === "challenge_recorded"
  ) {
    const directive = isGatewayRecord(resumed.directive)
      ? resumed.directive
      : {};
    return {
      status,
      result: compactGatewayRecord({
        ...visibleResult,
        intent_revision_id: businessString(resumed.intent_revision_id),
        directive_id: businessString(directive.directive_id),
      }),
    };
  }
  if (status === "failed") {
    return filterAgentCoreFailure(agentCore, resumed, false);
  }
  return { status, result: visibleResult };
}

const INTERACTION_INTENTS = new Set([
  "capability_question",
  "off_topic",
  "unsupported_request",
  "memory_update",
  "material_revision",
  "analysis_cancellation",
]);

const INTERACTION_TOPIC_RELATIONS = new Set([
  "inherit_current",
  "rejected",
  "material_revision",
  "analysis_cancellation",
]);

function projectMaterialRevisionContinuationForCustomer(
  value: unknown,
  sourceRunId: string,
) {
  if (!isGatewayRecord(value)) return null;
  const successorRunId = businessString(value.successor_run_id);
  const continuationRef = businessString(value.continuation_ref);
  if (
    value.schema_version !== "material-revision-continuation.v1"
    || value.source_run_id !== sourceRunId
    || !successorRunId
    || !continuationRef
  ) {
    return null;
  }
  return {
    source_run_id: sourceRunId,
    successor_run_id: successorRunId,
    events_url: `/api/runs/${successorRunId}/events`,
  };
}

const TOPIC_CHOICE_INTENTS = new Set([
  "new_topic",
  "follow_up",
  "mixed_question",
  "correction",
  "clarification_answer",
  "challenge",
]);

const TOPIC_CHOICE_RESULT_FIELDS = new Set([
  "schema_version",
  "intent",
  "response_text",
  "options",
  "recommended_topic_id",
  "allow_free_text",
]);

const TOPIC_CHOICE_OPTION_FIELDS = new Set([
  "topic_id",
  "label",
  "description",
]);

export function projectInteractionResultForCustomer(value: unknown) {
  if (!isGatewayRecord(value)) return null;
  const keys = Object.keys(value);
  if (value.schema_version === "typed-interaction.v1") {
    if (
      keys.length !== 3
      || !["schema_version", "intent", "response_text"].every(
        (field) => Object.prototype.hasOwnProperty.call(value, field),
      )
      || !INTERACTION_INTENTS.has(String(value.intent ?? ""))
      || !isTrimmedNonEmptyGatewayString(value.response_text)
    ) {
      return null;
    }
    return {
      schema_version: "typed-interaction.v1",
      intent: String(value.intent),
      response_text: value.response_text.trim(),
    };
  }
  if (
    value.schema_version !== "typed-topic-choice.v1"
    || keys.length !== TOPIC_CHOICE_RESULT_FIELDS.size
    || !keys.every((field) => TOPIC_CHOICE_RESULT_FIELDS.has(field))
    || !TOPIC_CHOICE_INTENTS.has(String(value.intent ?? ""))
    || !isTrimmedNonEmptyGatewayString(value.response_text)
    || !Array.isArray(value.options)
    || value.options.length < 2
    || value.options.length > 3
    || !isTrimmedNonEmptyGatewayString(value.recommended_topic_id)
    || value.allow_free_text !== true
  ) {
    return null;
  }
  const options = value.options.flatMap((option) => {
    if (!isGatewayRecord(option)) return [];
    const optionKeys = Object.keys(option);
    if (
      optionKeys.length !== TOPIC_CHOICE_OPTION_FIELDS.size
      || !optionKeys.every((field) => TOPIC_CHOICE_OPTION_FIELDS.has(field))
      || !isTrimmedNonEmptyGatewayString(option.topic_id)
      || !isTrimmedNonEmptyGatewayString(option.label)
      || !isTrimmedNonEmptyGatewayString(option.description)
    ) {
      return [];
    }
    return [{
      topic_id: option.topic_id.trim(),
      label: option.label.trim(),
      description: option.description.trim(),
    }];
  });
  const topicIds = options.map((option) => option.topic_id);
  const recommendedTopicId = value.recommended_topic_id.trim();
  if (
    options.length !== value.options.length
    || new Set(topicIds).size !== topicIds.length
    || !topicIds.includes(recommendedTopicId)
  ) {
    return null;
  }
  return {
    schema_version: "typed-topic-choice.v1",
    intent: String(value.intent),
    response_text: value.response_text.trim(),
    options,
    recommended_topic_id: recommendedTopicId,
    allow_free_text: true,
  };
}

const POST_EXECUTION_STATE_MATRIX: Record<
  string,
  { runStatus: string; publicationStatus: string; deliveryStatus: string }
> = {
  authority_sealed: {
    runStatus: "authority_sealed",
    publicationStatus: "not_ready",
    deliveryStatus: "pending",
  },
  narrative_ready: {
    runStatus: "narrative_ready",
    publicationStatus: "ready",
    deliveryStatus: "persisted",
  },
  completed: {
    runStatus: "completed",
    publicationStatus: "published",
    deliveryStatus: "published",
  },
  delivery_retryable_failed: {
    runStatus: "completed",
    publicationStatus: "ready",
    deliveryStatus: "retryable_failed",
  },
  delivery_permanently_failed: {
    runStatus: "completed",
    publicationStatus: "ready",
    deliveryStatus: "permanently_failed",
  },
  narrative_failed: {
    runStatus: "completed",
    publicationStatus: "not_ready",
    deliveryStatus: "pending",
  },
  publication_failed: {
    runStatus: "completed",
    publicationStatus: "failed",
    deliveryStatus: "pending",
  },
};

const POST_EXECUTION_REF_FIELDS = [
  "post_execution_result_ref",
  "post_execution_result_digest",
  "semantic_authority_result_ref",
  "semantic_authority_result_digest",
  "authority_bundle_ref",
  "authority_bundle_digest",
  "authority_transition_id",
  "claim_coverage_checkpoint_ref",
  "claim_coverage_checkpoint_digest",
  "claim_coverage_transition_id",
  "post_seal_failure_terminal_ref",
  "failure_record_ref",
  "failure_lifecycle_state_digest",
  "narrative_workflow_ref",
  "narrative_workflow_digest",
  "compose_transition_id",
  "publication_ref",
  "outbox_ref",
  "customer_payload_ref",
  "delivery_attempt_ref",
  "customer_publication_ref",
] as const;

export function projectPostExecutionStateForCustomer(
  value: unknown,
): SafePostExecutionState | null {
  if (!isGatewayRecord(value)) return null;
  const postExecutionStatus = businessString(value.post_execution_status);
  const expected = postExecutionStatus
    ? POST_EXECUTION_STATE_MATRIX[postExecutionStatus]
    : undefined;
  const refs = isGatewayRecord(value.publication_refs)
    ? value.publication_refs
    : null;
  if (
    !expected
    || (value.status !== undefined && value.status !== expected.runStatus)
    || value.publication_status !== expected.publicationStatus
    || value.delivery_status !== expected.deliveryStatus
    || !["complete", "boundary_only"].includes(
      String(value.analysis_status ?? ""),
    )
    || !refs
    || Object.keys(refs).length !== POST_EXECUTION_REF_FIELDS.length
    || !POST_EXECUTION_REF_FIELDS.every(
      (field) => Object.prototype.hasOwnProperty.call(refs, field),
    )
    || !POST_EXECUTION_REF_FIELDS.every(
      (field) => refs[field] === null || isNonEmptyGatewayString(refs[field]),
    )
    || !isGatewayDigest(refs.post_execution_result_digest)
    || !isNonEmptyGatewayString(refs.post_execution_result_ref)
    || !isGatewayDigest(refs.semantic_authority_result_digest)
    || !isNonEmptyGatewayString(refs.semantic_authority_result_ref)
    || !isGatewayDigest(refs.authority_bundle_digest)
    || !isNonEmptyGatewayString(refs.authority_bundle_ref)
    || !isNonEmptyGatewayString(refs.authority_transition_id)
    || !isGatewayDigest(refs.claim_coverage_checkpoint_digest)
    || refs.claim_coverage_checkpoint_ref
      !== `claim-coverage-checkpoint:sha256:${refs.claim_coverage_checkpoint_digest}`
    || !isNonEmptyGatewayString(refs.claim_coverage_transition_id)
  ) {
    return null;
  }
  const failureStatus = ["narrative_failed", "publication_failed"].includes(
    postExecutionStatus as string,
  );
  const failureRefsValid = isNonEmptyGatewayString(
    refs.post_seal_failure_terminal_ref,
  )
    && isNonEmptyGatewayString(refs.failure_record_ref)
    && isGatewayDigest(refs.failure_lifecycle_state_digest)
    && [
      "narrative_workflow_ref",
      "narrative_workflow_digest",
      "compose_transition_id",
      "publication_ref",
      "outbox_ref",
      "customer_payload_ref",
      "delivery_attempt_ref",
      "customer_publication_ref",
    ].every((field) => refs[field] === null);
  const noFailureRefs = refs.post_seal_failure_terminal_ref === null
    && refs.failure_record_ref === null
    && refs.failure_lifecycle_state_digest === null;
  const operationalFailure = projectOperationalFailure(
    value.operational_failure,
  );
  if (
    failureStatus !== (operationalFailure !== null)
    || (!failureStatus && value.operational_failure !== undefined)
    || (failureStatus && !failureRefsValid)
    || (!failureStatus && !noFailureRefs)
  ) return null;
  return {
    post_execution_status: postExecutionStatus as string,
    analysis_status: value.analysis_status as "complete" | "boundary_only",
    publication_status: expected.publicationStatus,
    delivery_status: expected.deliveryStatus,
    publication_refs: Object.fromEntries(
      POST_EXECUTION_REF_FIELDS.map((field) => [field, refs[field] as string | null]),
    ),
    ...(operationalFailure ? { operational_failure: operationalFailure } : {}),
  };
}

function projectOperationalFailure(value: unknown) {
  if (!isGatewayRecord(value)) return null;
  const fields = [
    "failure_ref",
    "layer",
    "kind",
    "retryability",
    "business_boundary",
  ] as const;
  if (
    Object.keys(value).length !== fields.length
    || !fields.every((field) => isNonEmptyGatewayString(value[field]))
    || !["narrative", "persistence"].includes(String(value.layer))
    || !["retryable", "not_retryable"].includes(String(value.retryability))
  ) {
    return null;
  }
  return {
    failure_ref: String(value.failure_ref),
    layer: value.layer as "narrative" | "persistence",
    kind: String(value.kind),
    retryability: value.retryability as "retryable" | "not_retryable",
    business_boundary: String(value.business_boundary),
  };
}

const CUSTOMER_AGENT_CORE_STATUSES = new Set([
  "started",
  "completed",
  "interaction_completed",
  "waiting_for_clarification",
  "planned",
  "evidence_ready",
  "authority_sealed",
  "narrative_ready",
  "material_revision_required",
  "run_cancelled",
  "challenge_recorded",
  "failed",
]);

function agentCoreStatusMismatch(wrapperStatus: string, innerStatus: string | undefined) {
  if (wrapperStatus === "failed") {
    return innerStatus !== undefined && innerStatus !== wrapperStatus;
  }
  if (
    wrapperStatus === "completed"
    || wrapperStatus === "interaction_completed"
    || wrapperStatus === "waiting_for_clarification"
    || wrapperStatus === "planned"
    || wrapperStatus === "evidence_ready"
    || wrapperStatus === "authority_sealed"
    || wrapperStatus === "narrative_ready"
    || wrapperStatus === "material_revision_required"
    || wrapperStatus === "run_cancelled"
    || wrapperStatus === "challenge_recorded"
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
  const question = businessString(value.question);
  const status = businessString(value.status);
  const rawOptions = Array.isArray(value.options) ? value.options : null;
  if (!question || !status || !rawOptions || !rawOptions.length) return null;
  const options = rawOptions.flatMap(filterBusinessClarificationOutcomeOption);
  if (options.length !== rawOptions.length) return null;
  return {
    question,
    status,
    allow_freeform: options.some(
      (option) => option.option_id === "tell_agent_differently"
    ),
    options,
    recommendation_reason: businessString(value.recommendation_reason),
  };
}

function filterBusinessClarificationState(value: unknown) {
  if (!isGatewayRecord(value)) return null;
  const runId = businessString(value.run_id);
  const topicId = businessString(value.topic_id);
  const question = businessString(value.question);
  const status = businessString(value.status);
  const rawOptions = Array.isArray(value.options) ? value.options : null;
  if (
    !runId
    || !topicId
    || !question
    || !status
    || !rawOptions
    || !rawOptions.length
  ) return null;
  const options = rawOptions.flatMap(filterBusinessClarificationStateOption);
  if (options.length !== rawOptions.length) return null;
  return compactGatewayRecord({
    run_id: runId,
    topic_id: topicId,
    question,
    status,
    options,
  });
}

function filterBusinessClarificationStateOption(
  value: unknown,
): Record<string, unknown>[] {
  if (
    !isGatewayRecord(value)
    || !hasExactClarificationOptionFields(value)
  ) return [];
  const optionId = businessString(value.option_id);
  const label = businessString(value.label);
  const description = businessString(value.description);
  if (
    !optionId
    || !label
    || !description
    || typeof value.recommended !== "boolean"
  ) return [];
  return [{
    option_id: optionId,
    label,
    description,
    recommended: value.recommended,
  }];
}

function filterBusinessClarificationOutcomeOption(
  value: unknown,
): Record<string, unknown>[] {
  if (
    !isGatewayRecord(value)
    || !hasExactClarificationOutcomeOptionFields(value)
  ) return [];
  const optionId = businessString(value.option_id);
  const label = businessString(value.label);
  const description = businessString(value.description);
  if (
    !optionId
    || !label
    || !description
    || typeof value.recommended !== "boolean"
  ) return [];
  return [{
    option_id: optionId,
    label,
    description,
    recommended: value.recommended,
  }];
}

const CLARIFICATION_OPTION_FIELDS = new Set([
  "option_id",
  "label",
  "description",
  "recommended",
]);

function hasExactClarificationOptionFields(value: Record<string, unknown>) {
  const fields = Object.keys(value);
  return fields.length === CLARIFICATION_OPTION_FIELDS.size
    && fields.every((field) => CLARIFICATION_OPTION_FIELDS.has(field));
}

function hasExactClarificationOutcomeOptionFields(
  value: Record<string, unknown>,
) {
  if (hasExactClarificationOptionFields(value)) return true;
  const fields = Object.keys(value);
  return fields.length === CLARIFICATION_OPTION_FIELDS.size + 1
    && fields.includes("typed_value")
    && isGatewayRecord(value.typed_value)
    && fields.every(
      (field) => field === "typed_value" || CLARIFICATION_OPTION_FIELDS.has(field)
    );
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

function auditTracePayload({
  run,
  customerPublication = null,
  publication = null,
  runNodes = [],
  evidenceRefs = [],
  resultRefs = [],
  executionSnapshots = [],
  auditEvents = [],
  verifierStatus = {},
}: {
  run: Record<string, unknown>;
  customerPublication?: unknown;
  publication?: SafePublicationRefs | null;
  runNodes?: Record<string, unknown>[];
  evidenceRefs?: Record<string, unknown>[];
  resultRefs?: Record<string, unknown>[];
  executionSnapshots?: Record<string, unknown>[];
  auditEvents?: Record<string, unknown>[];
  verifierStatus?: Record<string, unknown>;
}): RunAuditTrace {
  const projectedRun = projectCustomerAuditRun(run);
  const projectedPublication = customerPublication === null
    ? null
    : requireCustomerPublication(customerPublication);
  const projectedNodes = runNodes.map(projectCustomerAuditNode);
  const projectedEvidence = evidenceRefs.map(projectCustomerEvidenceLedgerEntry);
  const projectedResults = resultRefs.map(projectCustomerQueryResultRef);
  const projectedSnapshots = executionSnapshots.map(
    projectCustomerExecutionSnapshot,
  );
  const projectedAuditEvents = auditEvents.map(projectCustomerAuditEvent);
  const projectedVerifier = projectCustomerVerifierStatus(verifierStatus);
  return {
    run: projectedRun,
    customerPublication: projectedPublication,
    publication,
    runNodes: projectedNodes,
    evidenceRefs: projectedEvidence,
    resultRefs: projectedResults,
    executionSnapshots: projectedSnapshots,
    auditEvents: projectedAuditEvents,
    verifierStatus: projectedVerifier,
    traceCompleteness: {
      hasCustomerPublication: Boolean(projectedPublication),
      evidenceRefCount: projectedEvidence.length,
      resultRefCount: projectedResults.length,
      contractRefs: uniqueStrings(projectedResults.flatMap((row) => [
        row.query_contract_ref,
        row.analysis_contract_ref,
      ])),
      snapshotRefs: uniqueStrings(
        projectedResults.flatMap((row) => row.snapshot_refs as string[]),
      ),
      queryRefs: uniqueStrings(
        projectedResults.map((row) => row.query_contract_ref),
      ),
      resultRefs: uniqueStrings(
        projectedResults.map((row) => row.result_ref),
      ),
    },
  };
}

function projectCustomerAuditRun(value: Record<string, unknown>) {
  return {
    run_id: requiredProjectionString(value.run_id, "audit_trace_run_invalid"),
    status: validatedRunStatus(value.status),
    created_at: requiredProjectionTimestamp(
      value.created_at,
      "audit_trace_run_invalid",
    ),
    updated_at: requiredProjectionTimestamp(
      value.updated_at ?? value.created_at,
      "audit_trace_run_invalid",
    ),
  };
}

function projectCustomerAuditNode(value: Record<string, unknown>) {
  return {
    node_name: requiredProjectionString(
      value.node_name,
      "audit_trace_node_invalid",
    ),
    status: requiredProjectionString(value.status, "audit_trace_node_invalid"),
    started_at: optionalProjectionTimestamp(value.started_at),
    finished_at: optionalProjectionTimestamp(value.finished_at),
  };
}

function projectCustomerEvidenceLedgerEntry(value: Record<string, unknown>) {
  return {
    entry_ref: requiredProjectionString(
      value.entry_ref,
      "audit_trace_evidence_invalid",
    ),
    evidence_ref: requiredProjectionString(
      value.evidence_ref,
      "audit_trace_evidence_invalid",
    ),
    task_id: requiredProjectionString(
      value.task_id,
      "audit_trace_evidence_invalid",
    ),
    outcome_ref: requiredProjectionString(
      value.outcome_ref,
      "audit_trace_evidence_invalid",
    ),
    binding_record_ref: optionalProjectionString(value.binding_record_ref),
    execution_state: requiredProjectionString(
      value.execution_state,
      "audit_trace_evidence_invalid",
    ),
    evidence_kind: requiredProjectionString(
      value.evidence_kind,
      "audit_trace_evidence_invalid",
    ),
    data_contract_state: requiredProjectionString(
      value.data_contract_state,
      "audit_trace_evidence_invalid",
    ),
    maximum_claim_strength: requiredProjectionString(
      value.maximum_claim_strength,
      "audit_trace_evidence_invalid",
    ),
    result_membership_digest: requiredGatewayDigest(
      value.result_membership_digest,
      "audit_trace_evidence_invalid",
    ),
    completeness_membership_digest: requiredGatewayDigest(
      value.completeness_membership_digest,
      "audit_trace_evidence_invalid",
    ),
    created_at: requiredProjectionTimestamp(
      value.created_at,
      "audit_trace_evidence_invalid",
    ),
  };
}

function projectCustomerQueryResultRef(value: Record<string, unknown>) {
  return {
    result_ref: requiredProjectionString(
      value.result_ref,
      "audit_trace_query_invalid",
    ),
    query_contract_ref: requiredProjectionString(
      value.query_contract_ref,
      "audit_trace_query_invalid",
    ),
    analysis_contract_ref: requiredProjectionString(
      value.analysis_contract_ref,
      "audit_trace_query_invalid",
    ),
    query_contract_signature: requiredGatewayDigest(
      value.query_contract_signature,
      "audit_trace_query_invalid",
    ),
    analysis_contract_signature: requiredGatewayDigest(
      value.analysis_contract_signature,
      "audit_trace_query_invalid",
    ),
    execution_status: requiredProjectionString(
      value.execution_status,
      "audit_trace_query_invalid",
    ),
    query_hash: requiredGatewayDigest(
      value.query_hash,
      "audit_trace_query_invalid",
    ),
    completeness_report_ref: requiredProjectionString(
      value.completeness_report_ref,
      "audit_trace_query_invalid",
    ),
    query_record_ref: requiredProjectionString(
      value.query_record_ref,
      "audit_trace_query_invalid",
    ),
    query_record_digest: requiredGatewayDigest(
      value.query_record_digest,
      "audit_trace_query_invalid",
    ),
    completeness_record_ref: requiredProjectionString(
      value.completeness_record_ref,
      "audit_trace_query_invalid",
    ),
    completeness_digest: requiredGatewayDigest(
      value.completeness_digest,
      "audit_trace_query_invalid",
    ),
    completeness_status: requiredProjectionString(
      value.completeness_status,
      "audit_trace_query_invalid",
    ),
    analysis_readiness: requiredProjectionString(
      value.analysis_readiness,
      "audit_trace_query_invalid",
    ),
    row_count: requiredProjectionCount(
      value.row_count,
      "audit_trace_query_invalid",
    ),
    snapshot_refs: requiredProjectionStringArray(
      value.snapshot_refs,
      "audit_trace_query_invalid",
    ),
    created_at: requiredProjectionTimestamp(
      value.created_at,
      "audit_trace_query_invalid",
    ),
  };
}

function projectCustomerExecutionSnapshot(value: Record<string, unknown>) {
  return {
    execution_snapshot_ref: requiredProjectionString(
      value.execution_snapshot_ref,
      "audit_trace_execution_snapshot_invalid",
    ),
    authority_context_ref: requiredProjectionString(
      value.authority_context_ref,
      "audit_trace_execution_snapshot_invalid",
    ),
    plan_revision_id: requiredProjectionString(
      value.plan_revision_id,
      "audit_trace_execution_snapshot_invalid",
    ),
    stop_ref: requiredProjectionString(
      value.stop_ref,
      "audit_trace_execution_snapshot_invalid",
    ),
    outcome_set_digest: requiredGatewayDigest(
      value.outcome_set_digest,
      "audit_trace_execution_snapshot_invalid",
    ),
    evidence_ledger_digest: requiredGatewayDigest(
      value.evidence_ledger_digest,
      "audit_trace_execution_snapshot_invalid",
    ),
    content_digest: requiredGatewayDigest(
      value.content_digest,
      "audit_trace_execution_snapshot_invalid",
    ),
    created_at: requiredProjectionTimestamp(
      value.created_at,
      "audit_trace_execution_snapshot_invalid",
    ),
  };
}

function projectCustomerAuditEvent(value: Record<string, unknown>) {
  const eventType = requiredProjectionString(
    value.event_type,
    "audit_trace_event_invalid",
  );
  return {
    event_type: eventType,
    created_at: requiredProjectionTimestamp(
      value.created_at,
      "audit_trace_event_invalid",
    ),
    diagnostic: projectCustomerAuditPayload(eventType, value.payload),
  };
}

function projectCustomerVerifierStatus(value: Record<string, unknown>) {
  return {
    acceptedClaimCount: requiredProjectionCount(
      value.accepted_claim_count ?? 0,
      "audit_trace_verifier_invalid",
    ),
    vetoedClaimCount: requiredProjectionCount(
      value.vetoed_claim_count ?? 0,
      "audit_trace_verifier_invalid",
    ),
    vetoedBlockCount: requiredProjectionCount(
      value.vetoed_block_count ?? 0,
      "audit_trace_verifier_invalid",
    ),
    claimReportRefs: requiredProjectionStringArray(
      value.claim_report_refs ?? [],
      "audit_trace_verifier_invalid",
    ),
    blockReportRefs: requiredProjectionStringArray(
      value.block_report_refs ?? [],
      "audit_trace_verifier_invalid",
    ),
  };
}

function requiredProjectionString(value: unknown, error: string) {
  if (typeof value !== "string" || !value.trim()) throw gatewayError(error);
  return value;
}

function optionalProjectionString(value: unknown) {
  if (value === null || value === undefined) return null;
  return requiredProjectionString(value, "audit_trace_projection_invalid");
}

function requiredGatewayDigest(value: unknown, error: string) {
  if (!isGatewayDigest(value)) throw gatewayError(error);
  return value;
}

function requiredProjectionStringArray(value: unknown, error: string) {
  if (!Array.isArray(value)) throw gatewayError(error);
  return value.map((item) => requiredProjectionString(item, error));
}

function requiredProjectionCount(value: unknown, error: string) {
  const count = typeof value === "string" && /^\d+$/.test(value)
    ? Number(value)
    : value;
  if (!Number.isSafeInteger(count) || Number(count) < 0) {
    throw gatewayError(error);
  }
  return Number(count);
}

function requiredProjectionTimestamp(value: unknown, error: string) {
  const timestamp = customerTimestamp(value);
  if (!timestamp) throw gatewayError(error);
  return timestamp;
}

function optionalProjectionTimestamp(value: unknown) {
  if (value === null || value === undefined) return null;
  return requiredProjectionTimestamp(value, "audit_trace_projection_invalid");
}

function uniqueStrings(values: unknown[]) {
  return [...new Set(values.filter((value): value is string => typeof value === "string" && value.length > 0))];
}

function sameSet(left: string[], right: string[]) {
  return left.length === right.length && left.every((value) => right.includes(value));
}

function processEvent(eventType: string, payload: unknown): RunProcessEvent {
  if (eventType === "interaction_result_ready") {
    return {
      stage: "interaction",
      label: "交互响应已生成",
      summary: "已接收通过 typed interaction 合同生成的响应。",
      status: "interaction_completed",
    };
  }
  if (eventType === "plan_result_ready") {
    return {
      stage: "accepted_plan",
      label: "权威分析计划已确认",
      summary: "本轮分析范围、证据义务与能力任务已绑定到同一份计划。",
      status: "planned",
    };
  }
  if (eventType === "execution_result_ready") {
    return {
      stage: "evidence_summary",
      label: "证据执行完成",
      summary: "证据执行完成，待生成结论。",
      status: "evidence_ready",
    };
  }
  if (eventType === "claim_coverage_ready") {
    const coverage = isGatewayRecord(payload) ? payload : {};
    const decision = String(coverage.decision ?? "");
    return {
      stage: "claim_coverage",
      label: decision === "patch" ? "分析计划已扩展" : "结论覆盖已确认",
      summary: decision === "patch"
        ? "发现仍有可执行的高价值证据路径，已生成版本化计划补丁。"
        : "已检查各结论义务的证据覆盖，当前执行结果可以进入权威封存。",
      status: decision,
    };
  }
  if (eventType === "clarification_requested") {
    const question = firstClarificationQuestion(payload);
    return {
      stage: "question",
      label: "需要用户确认",
      summary: question || "需要确认业务口径后继续执行。",
      status: "waiting_for_user",
    };
  }
  if (eventType === "customer_publication_ready") {
    return {
      stage: "publication",
      label: "权威分析已发布",
      summary: "已发布经过证据、claim 和叙事校验的客户结果。",
      status: payloadStatus(payload),
    };
  }
  if (eventType === "post_execution_state") {
    const state = isGatewayRecord(payload) ? payload : {};
    return {
      stage: "post_execution",
      label: "分析后置状态已记录",
      summary: `发布：${String(state.publication_status ?? "")}；交付：${String(state.delivery_status ?? "")}。`,
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
  if (
    [
      "clarification_policy_gate",
      "generate_clarification",
      "persist_clarification",
    ].includes(nodeName)
  ) {
    return {
      stage: "question",
      label: "判断是否需要用户确认",
      summary: "已检查当前问题是否需要用户补充口径、范围或业务目标后继续。",
      status,
    };
  }
  if (["understand_business_intent", "decide_question_boundary"].includes(nodeName)) {
    return {
      stage: "intent",
      label: "理解业务问题",
      summary: "已把用户输入绑定为本轮可执行的业务问题和边界。",
      status,
    };
  }
  if (nodeName === "compile_authoritative_plan") {
    return {
      stage: "accepted_plan",
      label: "分析路径已验收",
      summary: "已确认本轮要执行的证据路径和可接受分支。",
      status,
    };
  }
  if (nodeName === "execute_capability_dag") {
    return {
      stage: "capability_progress",
      label: "证据路径推进",
      summary: capabilityProgressSummary(payload),
      status,
    };
  }
  if (nodeName === "evaluate_claim_coverage") {
    return {
      stage: "claim_coverage",
      label: "检查结论证据覆盖",
      summary: "已检查未解决结论是否仍有可执行、可验收的证据路径。",
      status,
    };
  }
  if (nodeName === "compile_plan_patch") {
    return {
      stage: "accepted_plan",
      label: "扩展分析计划",
      summary: "已把选中的增量证据路径编译为新的权威计划版本。",
      status,
    };
  }
  if (nodeName === "settle_claim_authority") {
    return {
      stage: "verifier_result",
      label: "结论权威已封存",
      summary: "已按证据来源、完整性和 claim 强度完成结论结算。",
      status,
    };
  }
  if (nodeName === "compose_claim_aware_narrative") {
    return {
      stage: "narrative",
      label: "权威叙事已生成",
      summary: "已在结论权威和表达边界内生成并校验业务回答。",
      status,
    };
  }
  if (nodeName === "deliver_publication") {
    return {
      stage: "publication",
      label: "客户发布已交付",
      summary: "已交付唯一客户安全投影，并记录发布与交付状态。",
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
  return isGatewayRecord(payload) ? businessString(payload.question) ?? "" : "";
}

function capabilityProgressSummary(payload: unknown) {
  if (!payload || typeof payload !== "object") return "已执行本轮所需证据路径。";
  const record = payload as Record<string, unknown>;
  const evidence = record.evidence ?? record.evidence_items ?? record.primary_evidence;
  if (Array.isArray(evidence) && evidence.length) return `已汇总 ${evidence.length} 条证据。`;
  return "已执行本轮所需证据路径。";
}

function payloadStatus(payload: unknown) {
  if (!payload || typeof payload !== "object") return undefined;
  const record = payload as Record<string, unknown>;
  const status = record.status ?? record.post_execution_status;
  return typeof status === "string" ? status : undefined;
}

function statusLabelForProcess(eventType: string, payload: unknown) {
  const status = payloadStatus(payload);
  if (status === "waiting_for_clarification") return "等待确认";
  if (status === "interaction_completed") return "交互已完成";
  if (status === "planned") return "分析计划已确认";
  if (status === "evidence_ready") return "结论覆盖已确认";
  if (status === "authority_sealed") return "权威结论已封存";
  if (status === "narrative_ready") return "叙事已校验";
  if (status === "completed") return "运行完成";
  if (status === "running_workflow") return "正在执行";
  return eventType;
}
