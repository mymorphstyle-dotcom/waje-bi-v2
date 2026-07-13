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
    throw new Error("WAJE_AGENT_CORE_COMMAND currently supports python3");
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

  if (options.forceInline || process.env.WAJE_AGENT_CORE_INLINE === "1") {
    return await runAgentCoreInline(args);
  }

  return await runAgentCoreDetached(args);
}

function runtimePermissionScopeForRole(role: string) {
  if (role === "analyst") return "analyst" as const;
  if (role === "data_owner_admin") return "admin" as const;
  return "viewer" as const;
}

function runAgentCoreDetached(args: string[]): Promise<AgentCoreResult> {
  return new Promise((resolve) => {
    const child = spawn("python3", args, {
      cwd: process.cwd(),
      detached: true,
      stdio: "ignore",
      env: process.env,
    });
    let settled = false;
    child.once("error", () => {
      if (settled) return;
      settled = true;
      resolve(agentCoreSpawnFailure());
    });
    child.once("spawn", () => {
      child.unref();
      if (settled) return;
      settled = true;
      resolve({ status: "started", command: "bi_agent.conversation.agent_core" });
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
      const processError = stderr.trim();
      resolve({
        status: code === 0 ? parsed.status : "failed",
        command: "bi_agent.conversation.agent_core",
        output,
        result: parsed.result,
        error: parsed.error
          || processError
          || (code === 0 ? undefined : "agent_core_process_failed"),
      });
    });
  });
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
