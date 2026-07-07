import { spawn } from "child_process";

export async function runAgentCore(
  threadId: string,
  runId: string,
  message: string,
  role = "analyst",
) {
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

  if (process.env.WAJE_AGENT_CORE_INLINE === "1") {
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

function runAgentCoreInline(args: string[]) {
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
      resolve({
        status: code === 0 ? "completed" : "failed",
        command: "bi_agent.conversation.agent_core",
        output: stdout.trim(),
        error: stderr.trim(),
      });
    });
  });
}
