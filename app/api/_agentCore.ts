import { spawn } from "child_process";

type AgentCoreResult = {
  status: string;
  command: string;
  output?: string;
  result?: unknown;
  error?: string;
};

type AgentCoreOptions = {
  runtimePermissionScope?: "viewer" | "analyst" | "admin";
  clarification?: {
    runId: string;
    answer: string;
    selectedOptionId?: string | null;
    source?: "user";
  };
  clarificationDispatch?: {
    sourceRunId: string;
    ownerId: string;
  };
  runDispatch?: {
    ownerId: string;
    leaseEpoch: number;
  };
  onDetachedWorkerExit?: () => void | Promise<void>;
  forceInline?: boolean;
};

export async function runAgentCore(
  threadId: string,
  runId: string,
  message: string,
  role = "business_reader",
  options: AgentCoreOptions = {},
): Promise<AgentCoreResult> {
  if (process.env.WAJE_AGENT_CORE_COMMAND && process.env.WAJE_AGENT_CORE_COMMAND !== "python3") {
    return agentCoreSpawnFailure();
  }
  const expectedPermissionScope = runtimePermissionScopeForRole(role);
  if (
    options.runtimePermissionScope &&
    options.runtimePermissionScope !== expectedPermissionScope
  ) {
    throw new Error("runtime_permission_scope_mismatch");
  }
  const runtimePermissionScope = options.runtimePermissionScope ?? expectedPermissionScope;
  const args = [
    "-m",
    "bi_agent.conversation.agent_core",
    "--thread-id",
    threadId,
    "--run-id",
    runId,
    "--message",
    message,
    "--role",
    role,
    "--runtime-permission-scope",
    runtimePermissionScope,
  ];
  if (options.clarification) {
    args.push("--clarification", JSON.stringify(options.clarification));
  }
  if (options.clarificationDispatch) {
    args.push(
      "--clarification-dispatch-source-run-id",
      options.clarificationDispatch.sourceRunId,
      "--clarification-dispatch-owner-id",
      options.clarificationDispatch.ownerId,
    );
  }
  if (options.runDispatch) {
    args.push(
      "--dispatch-owner-id",
      options.runDispatch.ownerId,
      "--dispatch-lease-epoch",
      String(options.runDispatch.leaseEpoch),
    );
  }

  if (options.forceInline || process.env.WAJE_AGENT_CORE_INLINE === "1") {
    return await runAgentCoreInline(args);
  }

  return await runAgentCoreDetached(args, options.onDetachedWorkerExit);
}

function runtimePermissionScopeForRole(role: string) {
  if (role === "analyst") return "analyst" as const;
  if (role === "data_owner_admin") return "admin" as const;
  return "viewer" as const;
}

function runAgentCoreDetached(
  args: string[],
  onWorkerExit?: () => void | Promise<void>,
): Promise<AgentCoreResult> {
  return new Promise((resolve) => {
    const child = spawn("python3", args, {
      cwd: process.cwd(),
      detached: true,
      stdio: ["ignore", "ignore", "ignore", "pipe"],
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
    startupPipe?.on("data", (chunk) => {
      acknowledgment += chunk.toString();
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

function runAgentCoreInline(args: string[]): Promise<AgentCoreResult> {
  return new Promise((resolve) => {
    const child = spawn("python3", args, {
      cwd: process.cwd(),
      env: process.env,
    });
    let stdout = "";
    let stderr = "";
    let settled = false;
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.once("error", () => {
      if (settled) return;
      settled = true;
      resolve(agentCoreSpawnFailure());
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      const output = stdout.trim();
      const parsed = parseAgentCoreOutput(output);
      resolve({
        status: code === 0 ? parsed.status : "failed",
        command: "bi_agent.conversation.agent_core",
        output,
        result: parsed.result,
        error: parsed.error
          || (code === 0 ? undefined : "agent_core_process_failed"),
      });
    });
  });
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
    || value === "completed_without_workflow"
    || value === "waiting_for_clarification"
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
    return isRecord(result.context_manifest) && isRecord(result.answer_package);
  }
  if (result.status === "completed_without_workflow") {
    return isRecord(result.context_manifest);
  }
  if (result.status === "waiting_for_clarification") {
    return isRecord(result.context_manifest) && isRecord(result.clarification);
  }
  return isNonEmptyString(result.failure_reason);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}
