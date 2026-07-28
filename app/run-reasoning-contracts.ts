export type TraceCapabilityOutcomeStatus =
  | "succeeded"
  | "unavailable"
  | "integrity_failed"
  | "technical_failed"
  | "skipped"
  | "superseded";

export type TraceCapabilityFailure = {
  layer: "query" | "capability" | "evidence" | "persistence";
  kind: string;
  integrityLevel: "expected_boundary" | "task" | "shared_authority";
  businessBoundary: string;
};

export type TraceReasoningIssueStatus =
  | "answered"
  | "partial"
  | "unresolved"
  | "omitted";

export type TraceReasoningIssue = {
  issueId: string;
  parentIssueId: string | null;
  question: string;
  targetClaimKind: string;
  status: TraceReasoningIssueStatus;
  answerText?: string;
  taskIds: string[];
  claimRefs: string[];
  usedClaimRefs: string[];
  limitationRefs: string[];
};

export type TraceReasoningQuery = {
  resultRef: string;
  queryContractRef: string;
  label: string;
  status: "waiting" | "running" | "completed" | "limited" | "failed";
  rowCount?: number;
  completedAt?: string;
};

export type TraceReasoningTask = {
  taskId: string;
  rank: number;
  taskKey: string;
  capabilityId: string;
  businessLabel: string;
  businessReadout?: string;
  status: TraceCapabilityOutcomeStatus | "not_started" | "unsettled";
  queryStatus: "completed" | "partial" | "failed" | "not_run";
  queryCount: number;
  queries: TraceReasoningQuery[];
  resultRefs: string[];
  evidenceRefs: string[];
  claimRefs: string[];
  issueIds: string[];
  dependencyTaskIds: string[];
  limitationRefs: string[];
  failure?: TraceCapabilityFailure;
};

export type TraceReasoningFact = {
  name: string;
  value: string;
};

export type TraceReasoningClaim = {
  proposedClaimRef: string;
  claimRef: string;
  claimKind: string;
  claimClass: string;
  source: "llm_proposed" | "runtime_derived";
  verificationStatus: "accepted" | "vetoed" | "unsettled";
  reasonCode?: string;
  summary: string;
  taskIds: string[];
  evidenceRefs: string[];
  issueIds: string[];
  facts: TraceReasoningFact[];
  usedInAnswer: boolean;
  answerBlockIds: string[];
  limitationRefs: string[];
};

export type TraceReasoningAnswerBlock = {
  blockId: string;
  role: string;
  text: string;
  claimRefs: string[];
  limitationRefs: string[];
};

export type TraceReasoning = {
  runId: string;
  businessUnderstanding: string;
  planRevisionId: string;
  issues: TraceReasoningIssue[];
  tasks: TraceReasoningTask[];
  claims: TraceReasoningClaim[];
  answerBlocks: TraceReasoningAnswerBlock[];
  counts: {
    taskTotal: number;
    taskCompleted: number;
    queryTotal: number;
    evidenceTotal: number;
    claimTotal: number;
    claimUsedInAnswer: number;
  };
};
