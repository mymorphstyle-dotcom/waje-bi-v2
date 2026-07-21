import { spawn } from "child_process";

import { wajePythonInvocation } from "./_pythonRuntime";

export type PendingActionResolutionInput = {
  actionRef: string;
  decision: "answered" | "approved" | "rejected";
  selectedOptionId: string | null;
  answerText: string;
};

export type GeneralAgentTurnInput = {
  threadId: string;
  actorId: string;
  operationId: string;
  message: string;
  pendingActionResolution?: PendingActionResolutionInput;
};

type GeneralAgentTurnProcessResult = {
  status: "started" | "completed" | "completed_with_limits" | "needs_input" | "working" | "failed";
  command: "bi_agent.runtime.general_agent_entry";
  result?: unknown;
  error?: string;
  technicalDetailRef?: string;
};

type GeneralAgentStartupControl = {
  schemaVersion: "general-agent-startup-control.v1";
  status: "running" | "failed";
  runId?: string;
  errorCode?: string;
  technicalDetailRef?: string;
};

export async function runGeneralAgentTurn(
  input: GeneralAgentTurnInput,
  options: { forceInline?: boolean } = {},
): Promise<GeneralAgentTurnProcessResult> {
  const args = [
    "-m",
    "bi_agent.runtime.general_agent_entry",
    "--command-json",
    JSON.stringify(input),
  ];
  if (options.forceInline || process.env.WAJE_GENERAL_AGENT_INLINE === "1") {
    return runInline(args);
  }
  return runDetached(args);
}

function runDetached(args: string[]): Promise<GeneralAgentTurnProcessResult> {
  return new Promise((resolve) => {
    const invocation = wajePythonInvocation(args);
    const child = spawn(invocation.command, invocation.args, {
      cwd: process.cwd(),
      detached: true,
      stdio: ["ignore", "ignore", "ignore", "pipe"],
      env: { ...process.env, WAJE_GENERAL_AGENT_STARTUP_ACK_FD: "3" },
    });
    let settled = false;
    let acknowledgment = "";
    const startupPipe = child.stdio[3];
    const configuredTimeout = Number(
      process.env.WAJE_GENERAL_AGENT_STARTUP_ACK_TIMEOUT_MS ?? "15000",
    );
    const timeoutMs = Number.isFinite(configuredTimeout) && configuredTimeout > 0
      ? configuredTimeout
      : 15000;
    const timer = setTimeout(() => settle(generalAgentFailure("general_agent_startup_failed")), timeoutMs);
    const settle = (result: GeneralAgentTurnProcessResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.unref();
      startupPipe?.destroy();
      resolve(result);
    };
    child.once("error", () => settle(generalAgentFailure("general_agent_spawn_failed")));
    startupPipe?.on("data", (chunk) => {
      acknowledgment += chunk.toString();
      while (acknowledgment.includes("\n")) {
        const newline = acknowledgment.indexOf("\n");
        const line = acknowledgment.slice(0, newline).trim();
        acknowledgment = acknowledgment.slice(newline + 1);
        const control = parseStartupControl(line);
        if (!control) continue;
        if (control.status === "running") {
          settle({
            status: "started",
            command: "bi_agent.runtime.general_agent_entry",
          });
          return;
        }
        settle(generalAgentFailure(
          control.errorCode || "general_agent_startup_failed",
          control.technicalDetailRef,
        ));
        return;
      }
    });
    child.once("close", () => {
      if (!settled) settle(generalAgentFailure("general_agent_startup_failed"));
    });
  });
}

function runInline(args: string[]): Promise<GeneralAgentTurnProcessResult> {
  return new Promise((resolve) => {
    const invocation = wajePythonInvocation(args);
    const child = spawn(invocation.command, invocation.args, {
      cwd: process.cwd(),
      env: process.env,
    });
    let stdout = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.once("error", () => resolve(generalAgentFailure("general_agent_spawn_failed")));
    child.once("close", (code) => {
      if (code !== 0) {
        resolve(generalAgentFailure("general_agent_process_failed"));
        return;
      }
      try {
        const result = JSON.parse(stdout.trim()) as Record<string, unknown>;
        const status = result.status;
        if (!isGeneralAgentStatus(status)) {
          resolve(generalAgentFailure("general_agent_output_invalid"));
          return;
        }
        resolve({
          status,
          command: "bi_agent.runtime.general_agent_entry",
          result,
        });
      } catch {
        resolve(generalAgentFailure("general_agent_output_malformed_json"));
      }
    });
  });
}

function generalAgentFailure(
  error: string,
  technicalDetailRef?: string,
): GeneralAgentTurnProcessResult {
  return {
    status: "failed",
    command: "bi_agent.runtime.general_agent_entry",
    error,
    ...(technicalDetailRef ? { technicalDetailRef } : {}),
  };
}

function parseStartupControl(line: string): GeneralAgentStartupControl | null {
  if (!line) return null;
  try {
    const value = JSON.parse(line) as Record<string, unknown>;
    if (
      value.schemaVersion !== "general-agent-startup-control.v1"
      || (value.status !== "running" && value.status !== "failed")
      || (value.errorCode !== undefined && typeof value.errorCode !== "string")
      || (value.technicalDetailRef !== undefined
        && typeof value.technicalDetailRef !== "string")
    ) return null;
    return value as GeneralAgentStartupControl;
  } catch {
    return null;
  }
}

function isGeneralAgentStatus(value: unknown): value is GeneralAgentTurnProcessResult["status"] {
  return value === "completed"
    || value === "completed_with_limits"
    || value === "needs_input"
    || value === "working"
    || value === "failed";
}
