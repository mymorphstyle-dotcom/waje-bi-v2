import { spawn } from "child_process";
import { wajePythonInvocation } from "./_pythonRuntime";

type AgentCoreResult = {
  status: string;
  command: string;
  output?: string;
  result?: unknown;
  error?: string;
};

type AgentCoreOptions = {
  topicSelection?: {
    sourceRunId: string;
    topicId: string;
  };
  topicChoiceAnswer?: {
    sourceRunId: string;
    answer: string;
  };
  clarification?: {
    sourceRunId: string;
    resolutionId: string;
    attemptRunId: string;
    answer: string;
    selectedOptionId: string | null;
    source: "user";
    retryAttempt: boolean;
  };
  runDispatch?: {
    dispatchId: string;
    ownerId: string;
    leaseEpoch: number;
  };
  intentRevisionContext?: {
    supersedes_intent_revision_id: string;
    superseded_plan_fields: string[];
    intent_revision_reason_ref: string;
    parent_transition_id: string;
  };
  onDetachedWorkerExit?: () => void | Promise<void>;
  forceInline?: boolean;
};

const AGENT_CORE_STARTUP_MAX_BYTES = 16 * 1024;
const AGENT_CORE_OUTPUT_MAX_BYTES = 4 * 1024 * 1024;

export async function runAgentCore(
  threadId: string,
  runId: string,
  message: string,
  actorId: string,
  options: AgentCoreOptions = {},
): Promise<AgentCoreResult> {
  const args = ["-m", "bi_agent.conversation.agent_core"];
  const commandJson = JSON.stringify({
    threadId,
    runId,
    message,
    userId: actorId,
    ...(options.clarification ? { clarification: options.clarification } : {}),
    ...(options.topicSelection ? { topicSelection: options.topicSelection } : {}),
    ...(options.topicChoiceAnswer
      ? { topicChoiceAnswer: options.topicChoiceAnswer }
      : {}),
    ...(options.runDispatch ? { runDispatch: options.runDispatch } : {}),
    ...(options.intentRevisionContext
      ? { intentRevisionContext: options.intentRevisionContext }
      : {}),
  });

  if (options.forceInline || process.env.WAJE_AGENT_CORE_INLINE === "1") {
    return await runAgentCoreInline(args, commandJson);
  }

  return await runAgentCoreDetached(
    args,
    commandJson,
    options.onDetachedWorkerExit,
  );
}

function runAgentCoreDetached(
  args: string[],
  commandJson: string,
  onWorkerExit?: () => void | Promise<void>,
): Promise<AgentCoreResult> {
  return new Promise((resolve) => {
    const invocation = wajePythonInvocation(args);
    const child = spawn(invocation.command, invocation.args, {
      cwd: process.cwd(),
      detached: true,
      stdio: ["pipe", "ignore", "ignore", "pipe"],
      env: { ...process.env, WAJE_AGENT_CORE_STARTUP_ACK_FD: "3" },
    });
    let settled = false;
    let acknowledgment = "";
    const startupPipe = child.stdio[3];
    const configuredTimeout = Number(
      process.env.WAJE_AGENT_CORE_STARTUP_ACK_TIMEOUT_MS ?? "15000",
    );
    const startupTimeoutMs = Number.isFinite(configuredTimeout) && configuredTimeout > 0
      ? configuredTimeout
      : 15000;
    const startupTimer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.unref();
      startupPipe?.destroy();
      resolve(agentCoreStartupFailure());
    }, startupTimeoutMs);
    child.once("error", () => {
      if (settled) return;
      settled = true;
      clearTimeout(startupTimer);
      resolve(agentCoreSpawnFailure());
    });
    const commandInput = child.stdin;
    if (!commandInput) {
      settled = true;
      clearTimeout(startupTimer);
      resolve(agentCoreCommandWriteFailure());
    } else {
      commandInput.on("error", () => {
        if (settled) return;
        settled = true;
        clearTimeout(startupTimer);
        resolve(agentCoreCommandWriteFailure());
      });
      commandInput.end(commandJson);
    }
    startupPipe?.on("data", (chunk) => {
      acknowledgment += chunk.toString();
      if (Buffer.byteLength(acknowledgment, "utf8") > AGENT_CORE_STARTUP_MAX_BYTES) {
        child.kill();
        if (settled) return;
        settled = true;
        clearTimeout(startupTimer);
        resolve(agentCoreStartupFailure());
        return;
      }
      if (!acknowledgment.includes("WAJE_AGENT_CORE_RUNNING\n")) return;
      if (settled) return;
      settled = true;
      clearTimeout(startupTimer);
      child.unref();
      startupPipe.destroy();
      resolve({ status: "started", command: "bi_agent.conversation.agent_core" });
    });
    child.once("close", () => {
      if (settled) {
        if (onWorkerExit) {
          try {
            void Promise.resolve(onWorkerExit()).catch(() => undefined);
          } catch {
            // The durable lease sweeper remains the fallback for observer failure.
          }
        }
        return;
      }
      settled = true;
      clearTimeout(startupTimer);
      resolve(agentCoreStartupFailure());
    });
  });
}

function runAgentCoreInline(
  args: string[],
  commandJson: string,
): Promise<AgentCoreResult> {
  return new Promise((resolve) => {
    const invocation = wajePythonInvocation(args);
    const child = spawn(invocation.command, invocation.args, {
      cwd: process.cwd(),
      env: process.env,
    });
    let stdout = "";
    let outputExceeded = false;
    let settled = false;
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
      if (Buffer.byteLength(stdout, "utf8") > AGENT_CORE_OUTPUT_MAX_BYTES) {
        outputExceeded = true;
        child.kill();
      }
    });
    child.stderr.resume();
    const commandInput = child.stdin;
    if (!commandInput) {
      settled = true;
      resolve(agentCoreCommandWriteFailure());
    } else {
      commandInput.on("error", () => {
        if (settled) return;
        settled = true;
        resolve(agentCoreCommandWriteFailure());
      });
      commandInput.end(commandJson);
    }
    child.once("error", () => {
      if (settled) return;
      settled = true;
      if (outputExceeded) {
        resolve(agentCoreOutputTooLargeFailure());
        return;
      }
      resolve(agentCoreSpawnFailure());
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      const output = stdout.trim();
      resolve(finalizeAgentCoreInlineResult(output, code));
    });
  });
}

export function finalizeAgentCoreInlineResult(
  output: string,
  exitCode: number | null,
): AgentCoreResult {
  const parsed = parseAgentCoreOutput(output);
  const typedFailure = !parsed.error && parsed.status === "failed";
  const processError = exitCode === 0 || typedFailure
    ? undefined
    : "agent_core_process_failed";
  return {
    status: exitCode === 0 || typedFailure ? parsed.status : "failed",
    command: "bi_agent.conversation.agent_core",
    output,
    result: parsed.result,
    error: parsed.error || processError,
  };
}

function agentCoreStartupFailure(): AgentCoreResult {
  return {
    status: "failed",
    command: "bi_agent.conversation.agent_core",
    output: "",
    result: null,
    error: "agent_core_startup_failed",
  };
}

function agentCoreSpawnFailure(): AgentCoreResult {
  return {
    status: "failed",
    command: "bi_agent.conversation.agent_core",
    output: "",
    result: null,
    error: "agent_core_spawn_failed",
  };
}

function agentCoreCommandWriteFailure(): AgentCoreResult {
  return {
    status: "failed",
    command: "bi_agent.conversation.agent_core",
    output: "",
    result: null,
    error: "agent_core_command_write_failed",
  };
}

function agentCoreOutputTooLargeFailure(): AgentCoreResult {
  return {
    status: "failed",
    command: "bi_agent.conversation.agent_core",
    output: "",
    result: null,
    error: "agent_core_output_too_large",
  };
}

export function parseAgentCoreOutput(output: string) {
  let result: unknown;
  try {
    result = JSON.parse(output);
  } catch {
    return {
      status: "failed",
      result: null,
      error: "agent_core_output_malformed_json",
    };
  }
  if (!isRecord(result) || !isKnownAgentCoreStatus(result.status)) {
    return {
      status: "failed",
      result: null,
      error: "agent_core_output_status_invalid",
    };
  }
  if (!isValidAgentCoreOutputShape(result)) {
    return {
      status: "failed",
      result: null,
      error: "agent_core_output_shape_invalid",
    };
  }
  return { status: result.status, result };
}

function isKnownAgentCoreStatus(value: unknown): value is string {
  return value === "completed"
    || value === "interaction_completed"
    || value === "waiting_for_clarification"
    || value === "planned"
    || value === "evidence_ready"
    || value === "authority_sealed"
    || value === "narrative_ready"
    || value === "material_revision_required"
    || value === "run_cancelled"
    || value === "challenge_recorded"
    || value === "failed";
}

function isValidAgentCoreOutputShape(result: Record<string, unknown>) {
  if (
    !isNonEmptyString(result.run_id)
    || !isNonEmptyString(result.turn_id)
    || !Object.prototype.hasOwnProperty.call(result, "topic_id")
    || !(result.topic_id === null || typeof result.topic_id === "string")
  ) {
    return false;
  }
  if (result.status === "completed") {
    return isRecord(result.context_manifest)
      && isValidPostExecutionTerminal(result);
  }
  if (result.status === "interaction_completed") {
    const interaction = isRecord(result.interaction_result)
      ? result.interaction_result
      : null;
    const topicRelation = String(result.topic_relation ?? "");
    return isRecord(result.context_manifest)
      && (
        topicRelation === "ask_topic_choice"
          ? isValidTopicChoiceInteractionResult(interaction)
          : INTERACTION_TOPIC_RELATIONS.has(topicRelation)
            && isValidInteractionResult(interaction)
      )
      && result.intent === interaction?.intent
      && (
        (interaction?.intent === "analysis_cancellation")
        === (topicRelation === "analysis_cancellation")
      )
      && isNonEmptyString(result.topic_relation);
  }
  if (result.status === "waiting_for_clarification") {
    return isRecord(result.context_manifest) && isRecord(result.clarification);
  }
  if (result.status === "planned") {
    return isRecord(result.context_manifest)
      && isValidPlannedResult(result.plan_result, result.run_id);
  }
  if (result.status === "evidence_ready") {
    return isRecord(result.context_manifest)
      && isValidAuthoritativeExecutionResult(
        result.execution_result,
        result.run_id,
      )
      && isValidClaimCoverageRefs(
        result.claim_coverage,
        result.execution_result,
      );
  }
  if (
    result.status === "authority_sealed"
    || result.status === "narrative_ready"
  ) {
    return isRecord(result.context_manifest)
      && isValidPostExecutionTerminal(result);
  }
  if (
    result.status === "material_revision_required"
    || result.status === "run_cancelled"
    || result.status === "challenge_recorded"
  ) {
    return isRecord(result.directive)
      && isRecord(result.durable_checkpoint)
      && typeof result.intent_revision_id === "string";
  }
  return isNonEmptyString(result.failure_reason);
}

const INTERACTION_RESULT_FIELDS = new Set([
  "schema_version",
  "intent",
  "response_text",
]);

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

const TOPIC_CHOICE_INTENTS = new Set([
  "new_topic",
  "follow_up",
  "mixed_question",
  "correction",
  "clarification_answer",
  "challenge",
]);

function isValidInteractionResult(value: unknown) {
  return isRecord(value)
    && hasExactFields(value, INTERACTION_RESULT_FIELDS)
    && value.schema_version === "typed-interaction.v1"
    && INTERACTION_INTENTS.has(String(value.intent ?? ""))
    && isTrimmedNonEmptyString(value.response_text);
}

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

function isValidTopicChoiceInteractionResult(value: unknown) {
  if (
    !isRecord(value)
    || !hasExactFields(value, TOPIC_CHOICE_RESULT_FIELDS)
    || value.schema_version !== "typed-topic-choice.v1"
    || !TOPIC_CHOICE_INTENTS.has(String(value.intent ?? ""))
    || !isTrimmedNonEmptyString(value.response_text)
    || !Array.isArray(value.options)
    || value.options.length < 2
    || value.options.length > 3
    || !isTrimmedNonEmptyString(value.recommended_topic_id)
    || value.allow_free_text !== true
  ) {
    return false;
  }
  const topicIds = value.options.flatMap((option) => {
    if (
      !isRecord(option)
      || !hasExactFields(option, TOPIC_CHOICE_OPTION_FIELDS)
      || !isTrimmedNonEmptyString(option.topic_id)
      || !isTrimmedNonEmptyString(option.label)
      || !isTrimmedNonEmptyString(option.description)
    ) {
      return [];
    }
    return [option.topic_id];
  });
  return topicIds.length === value.options.length
    && new Set(topicIds).size === topicIds.length
    && topicIds.includes(value.recommended_topic_id);
}

const POST_EXECUTION_TERMINALS: Record<
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

const POST_EXECUTION_REF_FIELDS = new Set([
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
]);

function isValidPostExecutionTerminal(result: Record<string, unknown>) {
  const postExecutionStatus = stringValue(result.post_execution_status);
  const expected = POST_EXECUTION_TERMINALS[postExecutionStatus];
  const refs = isRecord(result.publication_refs)
    ? result.publication_refs
    : null;
  if (
    !expected
    || result.status !== expected.runStatus
    || result.publication_status !== expected.publicationStatus
    || result.delivery_status !== expected.deliveryStatus
    || !["complete", "boundary_only"].includes(
      String(result.analysis_status ?? ""),
    )
    || !isValidPostExecutionRefs(refs)
  ) {
    return false;
  }
  const failureStatus = ["narrative_failed", "publication_failed"].includes(
    postExecutionStatus,
  );
  const failureRefsValid = refs !== null
    && isNonEmptyString(refs.post_seal_failure_terminal_ref)
    && isNonEmptyString(refs.failure_record_ref)
    && isDigest(refs.failure_lifecycle_state_digest)
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
  const noFailureRefs = refs !== null
    && refs.post_seal_failure_terminal_ref === null
    && refs.failure_record_ref === null
    && refs.failure_lifecycle_state_digest === null;
  if ((failureStatus && !failureRefsValid) || (!failureStatus && !noFailureRefs)) {
    return false;
  }
  if (postExecutionStatus === "completed") {
    return isRecord(result.customer_publication)
      && !Object.prototype.hasOwnProperty.call(result, "operational_failure");
  }
  if (failureStatus) {
    return isValidOperationalFailure(result.operational_failure)
      && !Object.prototype.hasOwnProperty.call(result, "customer_publication");
  }
  return !Object.prototype.hasOwnProperty.call(result, "customer_publication")
    && !Object.prototype.hasOwnProperty.call(result, "operational_failure");
}

function isValidOperationalFailure(value: unknown) {
  const fields = new Set([
    "failure_ref",
    "layer",
    "kind",
    "retryability",
    "business_boundary",
  ]);
  return isRecord(value)
    && hasExactFields(value, fields)
    && [...fields].every((field) => isTrimmedNonEmptyString(value[field]))
    && ["narrative", "persistence"].includes(String(value.layer))
    && ["retryable", "not_retryable"].includes(String(value.retryability));
}

function isValidPostExecutionRefs(value: unknown) {
  if (!isRecord(value) || !hasExactFields(value, POST_EXECUTION_REF_FIELDS)) {
    return false;
  }
  return [...POST_EXECUTION_REF_FIELDS].every(
    (field) => value[field] === null || isNonEmptyString(value[field]),
  )
    && isNonEmptyString(value.post_execution_result_ref)
    && isDigest(value.post_execution_result_digest)
    && isNonEmptyString(value.semantic_authority_result_ref)
    && isDigest(value.semantic_authority_result_digest)
    && isNonEmptyString(value.authority_bundle_ref)
    && isDigest(value.authority_bundle_digest)
    && isNonEmptyString(value.authority_transition_id)
    && value.claim_coverage_checkpoint_ref
      === `claim-coverage-checkpoint:sha256:${value.claim_coverage_checkpoint_digest}`
    && isDigest(value.claim_coverage_checkpoint_digest)
    && isNonEmptyString(value.claim_coverage_transition_id);
}

const PLAN_RESULT_FIELDS = new Set([
  "schema_version",
  "run_id",
  "run_attempt_id",
  "status",
  "intent_revision_id",
  "plan_patch_ref",
  "decision_ledger_position",
  "decision_refs",
  "authority_context",
  "planner_proposal",
  "proposal_admission_record",
  "plan_revision",
  "durable_checkpoint",
  "authority_refs",
  "llm_calls",
  "checkpoint_events",
]);

const PLAN_AUTHORITY_REF_FIELDS = new Set([
  "intent_revision_id",
  "authority_context_ref",
  "planner_proposal_id",
  "proposal_admission_id",
  "plan_revision_id",
  "accepted_transition_id",
]);

const EXECUTION_RESULT_FIELDS = new Set([
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

const EXECUTION_BUNDLE_FIELDS = new Set([
  "attempt",
  "outcome",
  "evidence_entries",
  "failure_records",
]);

const CAPABILITY_OUTCOME_STATUSES = new Set([
  "succeeded",
  "unavailable",
  "integrity_failed",
  "technical_failed",
  "skipped",
  "superseded",
]);

export function isValidAuthoritativeExecutionResult(
  value: unknown,
  runId: unknown,
) {
  if (
    !isRecord(value)
    || !hasExactFields(value, EXECUTION_RESULT_FIELDS)
    || value.schema_version !== "single-authority-phase03.v1"
    || value.status !== "evidence_ready"
    || !isNonEmptyString(runId)
    || value.run_attempt_id !== runId
    || !isNonEmptyString(value.intent_revision_id)
    || !isNonEmptyString(value.authority_context_ref)
    || !isNonEmptyString(value.plan_revision_id)
    || !isNonEmptyString(value.execution_snapshot_ref)
    || !isNonEmptyString(value.stop_ref)
    || !isNonEmptyString(value.transition_id)
    || !isDigest(value.bundle_set_digest)
    || !isDigest(value.content_digest)
    || value.authoritative_execution_result_ref
      !== `authoritative-execution-result:sha256:${value.content_digest}`
    || !isRecord(value.plan_revision)
    || !isRecord(value.execution_snapshot)
    || !isRecord(value.exploration_stop_record)
    || !isRecord(value.durable_transition)
    || !Array.isArray(value.capability_outcome_bundles)
  ) {
    return false;
  }

  const plan = value.plan_revision;
  const snapshot = value.execution_snapshot;
  const stop = value.exploration_stop_record;
  const transition = value.durable_transition;
  if (
    plan.run_attempt_id !== runId
    || plan.intent_revision_id !== value.intent_revision_id
    || plan.authority_context_ref !== value.authority_context_ref
    || plan.plan_revision_id !== value.plan_revision_id
    || !Array.isArray(plan.capability_tasks)
    || !Array.isArray(plan.claim_obligations)
    || snapshot.execution_snapshot_ref !== value.execution_snapshot_ref
    || snapshot.run_attempt_id !== runId
    || snapshot.authority_context_ref !== value.authority_context_ref
    || snapshot.plan_revision_id !== value.plan_revision_id
    || snapshot.stop_ref !== value.stop_ref
    || !isUniqueStringArray(snapshot.outcome_refs)
    || !isUniqueStringArray(snapshot.evidence_entry_refs)
    || !isUniqueStringArray(snapshot.failure_refs)
    || stop.stop_ref !== value.stop_ref
    || stop.run_attempt_id !== runId
    || stop.plan_revision_id !== value.plan_revision_id
    || !isUniqueStringArray(stop.evaluated_outcome_refs)
    || !isNonEmptyString(stop.reason)
    || !isNonNegativeInteger(stop.used_budget_units)
    || !(
      stop.hard_budget_limit === null
      || isNonNegativeInteger(stop.hard_budget_limit)
    )
    || transition.transition_id !== value.transition_id
    || transition.run_attempt_id !== runId
    || transition.intent_revision_id !== value.intent_revision_id
    || transition.node_name !== "execute_capability_dag"
    || transition.status !== "succeeded"
    || transition.acceptance_state !== "accepted"
    || transition.next_transition !== "phase03_evidence_bound"
    || !isDigest(transition.input_digest)
    || !isDigest(transition.output_digest)
  ) {
    return false;
  }

  const obligations = plan.claim_obligations;
  const obligationIds = new Set<string>();
  for (const obligation of obligations) {
    const evidenceRequirement = isRecord(obligation)
      ? obligation.evidence_requirement
      : null;
    if (
      !isRecord(obligation)
      || !isNonEmptyString(obligation.obligation_id)
      || obligationIds.has(obligation.obligation_id)
      || !isNonEmptyString(obligation.claim_kind)
      || !isNonEmptyString(obligation.role)
      || !isRecord(evidenceRequirement)
      || evidenceRequirement.operator !== "any_of"
      || !isUniqueStringArray(evidenceRequirement.evidence_kinds)
      || evidenceRequirement.evidence_kinds.length === 0
    ) {
      return false;
    }
    obligationIds.add(obligation.obligation_id);
  }

  const tasks = plan.capability_tasks;
  const taskById = new Map<string, Record<string, unknown>>();
  for (const task of tasks) {
    if (
      !isRecord(task)
      || !isNonEmptyString(task.task_id)
      || taskById.has(task.task_id)
      || task.plan_revision_id !== value.plan_revision_id
      || task.authority_context_ref !== value.authority_context_ref
      || !isNonEmptyString(task.capability_id)
      || !Array.isArray(task.obligation_edges)
      || !isUniqueStringArray(task.supports_obligation_ids)
    ) {
      return false;
    }
    const edgeIds = new Set<string>();
    for (const edge of task.obligation_edges) {
      if (
        !isRecord(edge)
        || !isNonEmptyString(edge.obligation_id)
        || edgeIds.has(edge.obligation_id)
        || !obligationIds.has(edge.obligation_id)
        || typeof edge.required !== "boolean"
      ) {
        return false;
      }
      edgeIds.add(edge.obligation_id);
    }
    if (
      task.supports_obligation_ids.some(
        (obligationId) => !edgeIds.has(obligationId),
      )
    ) {
      return false;
    }
    taskById.set(task.task_id, task);
  }

  const outcomeRefs = new Set<string>();
  const evidenceEntryRefs = new Set<string>();
  const failureRefs = new Set<string>();
  const attemptedTaskIds = new Set<string>();
  const attemptIds = new Set<string>();
  let usedBudgetUnits = 0;
  for (const bundle of value.capability_outcome_bundles) {
    if (
      !isRecord(bundle)
      || !hasExactFields(bundle, EXECUTION_BUNDLE_FIELDS)
      || !isRecord(bundle.attempt)
      || !isRecord(bundle.outcome)
      || !Array.isArray(bundle.evidence_entries)
      || !Array.isArray(bundle.failure_records)
    ) {
      return false;
    }
    const attempt = bundle.attempt;
    const outcome = bundle.outcome;
    const taskId = stringValue(attempt.task_id);
    const attemptId = stringValue(attempt.attempt_id);
    const outcomeRef = stringValue(outcome.outcome_ref);
    const task = taskById.get(taskId);
    if (
      !task
      || !attemptId
      || !outcomeRef
      || attemptedTaskIds.has(taskId)
      || attemptIds.has(attemptId)
      || outcomeRefs.has(outcomeRef)
      || attempt.run_attempt_id !== runId
      || attempt.intent_revision_id !== value.intent_revision_id
      || attempt.plan_revision_id !== value.plan_revision_id
      || !isPositiveInteger(attempt.execution_attempt)
      || !isDigest(attempt.input_digest)
      || outcome.run_attempt_id !== runId
      || outcome.plan_revision_id !== value.plan_revision_id
      || outcome.task_id !== taskId
      || outcome.attempt_id !== attemptId
      || outcome.input_digest !== attempt.input_digest
      || !CAPABILITY_OUTCOME_STATUSES.has(String(outcome.status ?? ""))
      || !isUniqueStringArray(outcome.evidence_refs)
      || !isUniqueStringArray(outcome.affected_obligation_ids)
      || !isUniqueStringArray(outcome.limitation_refs)
      || !isDigest(outcome.output_digest)
      || !isNonNegativeInteger(outcome.budget_units)
      || !(
        outcome.failure_ref === null
        || isNonEmptyString(outcome.failure_ref)
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
    const supportedObligations = new Set(task.supports_obligation_ids as string[]);
    if (
      outcome.affected_obligation_ids.some(
        (obligationId) => !supportedObligations.has(obligationId),
      )
    ) {
      return false;
    }
    attemptedTaskIds.add(taskId);
    attemptIds.add(attemptId);
    outcomeRefs.add(outcomeRef);
    usedBudgetUnits += Number(outcome.budget_units);

    const bundleEvidenceRefs = new Set<string>();
    for (const rawEntry of bundle.evidence_entries) {
      if (!isRecord(rawEntry)) return false;
      const entryRef = stringValue(rawEntry.entry_ref);
      const evidenceRef = stringValue(rawEntry.evidence_ref);
      if (
        !entryRef
        || !evidenceRef
        || evidenceEntryRefs.has(entryRef)
        || bundleEvidenceRefs.has(evidenceRef)
        || rawEntry.run_attempt_id !== runId
        || rawEntry.authority_context_ref !== value.authority_context_ref
        || rawEntry.plan_revision_id !== value.plan_revision_id
        || rawEntry.task_id !== taskId
        || rawEntry.outcome_ref !== outcomeRef
        || !isNonEmptyString(rawEntry.execution_state)
        || !isNonEmptyString(rawEntry.evidence_kind)
        || !isNonEmptyString(rawEntry.data_contract_state)
        || !isNonEmptyString(rawEntry.evidence_strength)
        || !isNonEmptyString(rawEntry.maximum_claim_strength)
        || !isNonEmptyString(rawEntry.scope)
        || !isUniqueStringArray(rawEntry.supported_claim_kinds)
        || !isUniqueStringArray(rawEntry.window_refs)
        || !isUniqueStringArray(rawEntry.result_refs)
        || !isUniqueStringArray(rawEntry.completeness_report_refs)
        || !isUniqueStringArray(rawEntry.dimension_path)
        || typeof rawEntry.hierarchy_qualified !== "boolean"
        || !isUniqueStringArray(rawEntry.limitation_refs)
      ) {
        return false;
      }
      evidenceEntryRefs.add(entryRef);
      bundleEvidenceRefs.add(evidenceRef);
    }
    if (!sameStringSet(outcome.evidence_refs, bundleEvidenceRefs)) return false;

    const bundleFailureRefs = new Set<string>();
    for (const rawFailure of bundle.failure_records) {
      if (!isRecord(rawFailure)) return false;
      const failureRef = stringValue(rawFailure.failure_ref);
      if (
        !failureRef
        || failureRefs.has(failureRef)
        || bundleFailureRefs.has(failureRef)
        || rawFailure.run_attempt_id !== runId
        || rawFailure.plan_revision_id !== value.plan_revision_id
        || rawFailure.task_id !== taskId
        || rawFailure.attempt_id !== attemptId
        || !isNonEmptyString(rawFailure.scope)
        || !isNonEmptyString(rawFailure.integrity_level)
        || !isNonEmptyString(rawFailure.retryability)
        || typeof rawFailure.user_actionable !== "boolean"
        || !isNonEmptyString(rawFailure.business_boundary)
      ) {
        return false;
      }
      failureRefs.add(failureRef);
      bundleFailureRefs.add(failureRef);
    }
    const expectedFailureRefs = outcome.failure_ref === null
      ? new Set<string>()
      : new Set([outcome.failure_ref as string]);
    if (!sameStringSet([...bundleFailureRefs], expectedFailureRefs)) return false;
  }

  return usedBudgetUnits === stop.used_budget_units
    && (
      stop.reason !== "plan_exhausted"
      || attemptedTaskIds.size === taskById.size
    )
    && (
      stop.reason !== "hard_budget_reached"
      || (
        stop.hard_budget_limit !== null
        && usedBudgetUnits >= Number(stop.hard_budget_limit)
      )
    )
    && sameStringSet(snapshot.outcome_refs, outcomeRefs)
    && sameStringSet(stop.evaluated_outcome_refs, outcomeRefs)
    && sameStringSet(snapshot.evidence_entry_refs, evidenceEntryRefs)
    && sameStringSet(snapshot.failure_refs, failureRefs);
}

function isValidPlannedResult(value: unknown, runId: unknown) {
  if (!isRecord(value) || !hasExactFields(value, PLAN_RESULT_FIELDS)) return false;
  const authorityRefs = value.authority_refs;
  if (
    value.schema_version !== "single-authority-phase02.v2"
    || value.status !== "planned"
    || value.run_id !== runId
    || value.run_attempt_id !== runId
    || !isNonEmptyString(value.intent_revision_id)
    || !Number.isSafeInteger(value.decision_ledger_position)
    || Number(value.decision_ledger_position) < 0
    || !isStringArray(value.decision_refs)
    || !isRecord(value.authority_context)
    || !isRecord(value.planner_proposal)
    || !isRecord(value.proposal_admission_record)
    || !isRecord(value.plan_revision)
    || !isRecord(value.durable_checkpoint)
    || !Array.isArray(value.llm_calls)
    || !Array.isArray(value.checkpoint_events)
    || !isRecord(authorityRefs)
    || !hasExactFields(authorityRefs, PLAN_AUTHORITY_REF_FIELDS)
  ) {
    return false;
  }
  const supersededPlanId = value.plan_revision.supersedes_plan_revision_id;
  if (
    (supersededPlanId === null && value.plan_patch_ref !== null)
    || (
      isNonEmptyString(supersededPlanId)
      && !isContentAddressedPlanPatchRef(value.plan_patch_ref)
    )
    || !(supersededPlanId === null || isNonEmptyString(supersededPlanId))
  ) {
    return false;
  }
  return [...PLAN_AUTHORITY_REF_FIELDS].every(
    (field) => isNonEmptyString(authorityRefs[field]),
  ) && authorityRefs.intent_revision_id === value.intent_revision_id;
}

const CLAIM_COVERAGE_REF_FIELDS = new Set([
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
]);

function isValidClaimCoverageRefs(value: unknown, execution: unknown) {
  if (
    !isRecord(value)
    || !hasExactFields(value, CLAIM_COVERAGE_REF_FIELDS)
    || !isRecord(execution)
    || value.schema_version !== "claim-coverage-checkpoint.v1"
    || value.source_plan_revision_id !== execution.plan_revision_id
    || value.source_execution_result_ref
      !== execution.authoritative_execution_result_ref
    || value.decision !== "seal"
    || value.plan_patch_ref !== null
    || !isDigest(value.claim_coverage_checkpoint_digest)
    || value.claim_coverage_checkpoint_ref
      !== `claim-coverage-checkpoint:sha256:${value.claim_coverage_checkpoint_digest}`
  ) {
    return false;
  }
  return [
    "source_plan_revision_id",
    "source_execution_result_ref",
    "claim_coverage_evaluation_ref",
    "plan_expansion_decision_ref",
    "accepted_transition_id",
  ].every((field) => isNonEmptyString(value[field]));
}

function isContentAddressedPlanPatchRef(value: unknown) {
  return typeof value === "string"
    && /^plan-patch:sha256:[0-9a-f]{64}$/.test(value);
}

function hasExactFields(value: Record<string, unknown>, fields: Set<string>) {
  const keys = Object.keys(value);
  return keys.length === fields.size && keys.every((key) => fields.has(key));
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(isNonEmptyString);
}

function isUniqueStringArray(value: unknown): value is string[] {
  return isStringArray(value) && new Set(value).size === value.length;
}

function sameStringSet(
  values: string[],
  expected: ReadonlySet<string>,
) {
  return values.length === expected.size
    && values.every((value) => expected.has(value));
}

function isDigest(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) >= 0;
}

function isPositiveInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) > 0;
}

function stringValue(value: unknown) {
  return isNonEmptyString(value) ? value : "";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isTrimmedNonEmptyString(value: unknown): value is string {
  return isNonEmptyString(value) && value === value.trim();
}
