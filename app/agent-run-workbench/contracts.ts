export type TraceOwner = "LLM" | "本地系统" | "混合" | "用户" | "未知";

export type TraceCompleteness = "known" | "unknown" | "incomplete";

export type TraceLifecycleOutcome =
  | "complete"
  | "running"
  | "checkpoint"
  | "pending"
  | "failed"
  | "blocked"
  | "not_applicable"
  | "unknown";

export type TraceLifecycleState = {
  outcome: TraceLifecycleOutcome;
  status: string | null;
  completeness: TraceCompleteness;
  detail?: string;
  at?: string;
};

export type TraceRunOutcome =
  | "completed"
  | "running"
  | "checkpoint"
  | "interaction_completed"
  | "delivery_pending"
  | "delivery_failed"
  | "failed"
  | "waiting"
  | "withheld"
  | "published"
  | "unknown";

export type TraceNodeOutcome =
  | "completed"
  | "failed"
  | "waiting"
  | "skipped"
  | "unknown";

export type TraceCard = {
  label: string;
  value: string;
  detail?: string;
};

export type TraceEvidenceExecutionState =
  | "available"
  | "unavailable"
  | "integrity_failed"
  | "technical_failed";

export type TraceEvidencePlanState = "active" | "superseded";
export type TraceEvidenceBindingState = "bound" | "unsettled";

export type TraceCapabilityOutcomeStatus =
  | "succeeded"
  | "unavailable"
  | "integrity_failed"
  | "technical_failed"
  | "skipped"
  | "superseded";

export type TraceCapabilityRetryability =
  | "never"
  | "same_input"
  | "replan_required";

export type TraceCapabilityFailure = {
  layer: "query" | "capability" | "evidence" | "persistence";
  kind: string;
  integrityLevel: "expected_boundary" | "task" | "shared_authority";
  businessBoundary: string;
};

type TraceAcceptedTaskIdentity = {
  taskId: string;
  planRevisionId: string;
  capabilityId: string;
  taskKey: string;
};

export type TraceAcceptedTaskExecution =
  | { state: "not_started" }
  | { state: "unsettled" }
  | {
    state: "settled";
    outcomeRef: string;
    status: TraceCapabilityOutcomeStatus;
    retryability: TraceCapabilityRetryability;
    limitationRefs: string[];
    failure?: TraceCapabilityFailure;
  };

export type TraceAcceptedTask = TraceAcceptedTaskIdentity & {
  execution: TraceAcceptedTaskExecution;
};

export type TraceEvidence = {
  capability: string;
  label: string;
  detail: string;
  strength: string;
  executionState: TraceEvidenceExecutionState;
  planRevisionId: string;
  planState: TraceEvidencePlanState;
  taskId: string;
  bindingState: TraceEvidenceBindingState;
  executionTransitionAttemptId?: string;
  limitations: string[];
  limitationsCompleteness: TraceCompleteness;
  evidenceRef: string;
};

export type TraceClaim = {
  claimRef: string;
  claimClass?: string;
  status?: string;
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
  outcome: TraceNodeOutcome;
  route?: string;
  durationMs?: number;
  startedAt?: string;
  finishedAt?: string;
  evidenceRefs?: string[];
  evidenceCompleteness?: TraceCompleteness;
  claimRefs?: string[];
  summary: string;
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
  evidence: TraceEvidence[];
};

export type TraceHumanReview = {
  status: "pending" | "reviewed" | "revision_requested" | "not_available";
  evaluationCount: number;
  latest?: {
    reviewerRef: string;
    scores: Record<string, number>;
    humanReasons: Record<string, string>;
    result: "retain_publication" | "request_independent_narrative_attempt";
    reviewedAt: string;
  };
};

export type TraceRun = {
  id: string;
  label: string;
  question: string;
  status: string;
  runOutcome: TraceRunOutcome;
  runMode: "event_replay" | "static_snapshot";
  runId: string;
  generatedAt?: number;
  summaryCards: TraceCard[];
  businessThreads?: TraceCard[];
  traceClaims: TraceClaim[];
  traceEvidence: TraceEvidence[];
  messages?: TraceMessage[];
  answer?: TraceAnswer;
  humanReview: TraceHumanReview;
  lifecycle: {
    execution: TraceLifecycleState;
    verifier: TraceLifecycleState;
    publication: TraceLifecycleState;
    delivery: TraceLifecycleState;
  };
  traceCompleteness: {
    chronology: TraceCompleteness;
    llmCalls: TraceCompleteness;
    acceptedGraph: TraceCompleteness;
    claims: TraceCompleteness;
    evidence: TraceCompleteness;
    timing: TraceCompleteness;
  };
  timing: {
    actualDurationMs?: number;
    playbackDurationMs?: number;
    completeness: TraceCompleteness;
  };
  processSummary: {
    checkpointCount: number;
    llmCallCount?: number;
    acceptedGraph?: TraceAcceptedTask[];
    verifierStatus: string | null;
    nodes: TraceNode[];
  };
};
