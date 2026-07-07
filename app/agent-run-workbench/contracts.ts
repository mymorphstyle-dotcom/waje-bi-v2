export type TraceOwner = "LLM" | "本地系统";

export type TraceCard = {
  label: string;
  value: string;
  detail?: string;
};

export type TraceEvidence = {
  capability: string;
  label: string;
  detail: string;
  strength: string;
  limitations: string[];
  evidenceRef?: string;
};

export type TraceClaim = {
  text: string;
  scope?: string;
  timeWindow?: string;
  numbers?: Record<string, number>;
  evidenceRefs: string[];
};

export type TraceNode = {
  id: string;
  index: number;
  node: string;
  label: string;
  owner: TraceOwner;
  status: string;
  route?: string;
  durationMs?: number;
  startedAt?: string;
  finishedAt?: string;
  summary: string;
  audit?: unknown;
};

export type TraceMessage = {
  id: string;
  role: "user" | "assistant" | "tool";
  text: string;
  title?: string;
  nodeId?: string;
};

export type TraceAnswer = {
  status: string;
  answerText: string;
  claims: TraceClaim[];
  limitations: string[];
  repairPath: string;
  stats: TraceCard[];
  evidence: TraceEvidence[];
};

export type TraceRun = {
  id: string;
  label: string;
  question: string;
  status: string;
  runId: string;
  generatedAt?: number;
  summaryCards: TraceCard[];
  businessThreads?: TraceCard[];
  traceClaims: TraceClaim[];
  traceEvidence: TraceEvidence[];
  messages?: TraceMessage[];
  answer?: TraceAnswer;
  timing: {
    actualDurationMs: number;
    playbackDurationMs: number;
  };
  processSummary: {
    checkpointCount: number;
    llmCallCount: number;
    acceptedGraph: string[];
    verifierStatus: string;
    sourceArtifact?: string;
    debugStage?: string;
    nodes: TraceNode[];
  };
};
