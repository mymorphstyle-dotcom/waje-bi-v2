import { spawn } from "child_process";

type AgentCoreResult = {
  status: string;
  command: string;
  output?: string;
  result?: unknown;
  error?: string;
};

type AgentCoreOptions = {
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
  role = "analyst",
  options: AgentCoreOptions = {},
): Promise<AgentCoreResult> {
  if (process.env.WAJE_AGENT_CORE_COMMAND && process.env.WAJE_AGENT_CORE_COMMAND !== "python3") {
    throw new Error("WAJE_AGENT_CORE_COMMAND currently supports python3");
  }
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
  ];
  if (options.clarification) {
    args.push("--clarification", JSON.stringify(options.clarification));
  }

  if (options.forceInline || process.env.WAJE_AGENT_CORE_INLINE === "1") {
    return await runAgentCoreInline(args);
  }

  const child = spawn("python3", args, {
    cwd: process.cwd(),
    detached: true,
    stdio: "ignore",
    env: process.env,
  });
  child.unref();
  return { status: "started", command: "bi_agent.conversation.agent_core" };
}

function runAgentCoreInline(args: string[]): Promise<AgentCoreResult> {
  return new Promise((resolve) => {
    const child = spawn("python3", args, {
      cwd: process.cwd(),
      env: process.env,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("close", (code) => {
      const output = stdout.trim();
      const parsed = parseAgentCoreOutput(output);
      resolve({
        status: code === 0 ? parsed.status : "failed",
        command: "bi_agent.conversation.agent_core",
        output,
        result: parsed.result,
        error: stderr.trim(),
      });
    });
  });
}

function parseAgentCoreOutput(output: string) {
  try {
    const result = JSON.parse(output);
    if (
      result?.status === "completed" ||
      result?.status === "completed_without_workflow" ||
      result?.status === "waiting_for_clarification"
    ) {
      return { status: result.status, result };
    }
  } catch {
    return { status: "completed", result: null };
  }
  return { status: "completed", result: null };
}
