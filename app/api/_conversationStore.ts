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
  status: "queued" | "running" | "completed";
  createdAt: string;
};

type MemoryProposalRecord = {
  id: string;
  threadId: string;
  text: string;
  status: "proposed" | "accepted" | "rejected";
  createdAt: string;
};

type RunEvent = {
  event: string;
  runId: string;
  threadId?: string;
  payload?: unknown;
};

type MemoryStore = {
  threads: Map<string, ThreadRecord>;
  runs: Map<string, RunRecord>;
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
    },
  ];
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
