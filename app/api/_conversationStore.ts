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

type ConversationStore = {
  threads: Map<string, ThreadRecord>;
  runs: Map<string, RunRecord>;
  memoryProposals: Map<string, MemoryProposalRecord>;
};

const globalStore = globalThis as typeof globalThis & {
  __wajeConversationStore?: ConversationStore;
};

export function conversationStore() {
  globalStore.__wajeConversationStore ??= {
    threads: new Map(),
    runs: new Map(),
    memoryProposals: new Map(),
  };
  return globalStore.__wajeConversationStore;
}

export function createThread(ownerId = "local-user") {
  const store = conversationStore();
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

export function requireThread(threadId: string) {
  const store = conversationStore();
  const thread = store.threads.get(threadId);
  if (!thread) throw new Error("thread_not_found");
  return thread;
}

export function createRun(threadId: string) {
  const store = conversationStore();
  const run: RunRecord = {
    id: `run-${crypto.randomUUID()}`,
    threadId,
    status: "queued",
    createdAt: new Date().toISOString(),
  };
  store.runs.set(run.id, run);
  return run;
}

export function addUserMessage(threadId: string, text: string) {
  const thread = requireThread(threadId);
  const message: MessageRecord = {
    id: `message-${crypto.randomUUID()}`,
    role: "user",
    text,
    createdAt: new Date().toISOString(),
  };
  thread.messages.push(message);
  return message;
}

export function createMemoryProposal(threadId: string, text: string) {
  const store = conversationStore();
  const proposal: MemoryProposalRecord = {
    id: `memory-proposal-${crypto.randomUUID()}`,
    threadId,
    text,
    status: "proposed",
    createdAt: new Date().toISOString(),
  };
  store.memoryProposals.set(proposal.id, proposal);
  return proposal;
}

export function updateMemoryProposal(proposalId: string, status: "accepted" | "rejected") {
  const proposal = conversationStore().memoryProposals.get(proposalId);
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
