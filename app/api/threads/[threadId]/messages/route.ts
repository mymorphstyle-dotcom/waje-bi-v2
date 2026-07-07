import { NextRequest, NextResponse } from "next/server";
import { spawn } from "child_process";

import { addUserMessage, createMemoryProposal, createRun, jsonError } from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ threadId: string }> };

export async function POST(request: NextRequest, context: RouteContext) {
  const { threadId } = await context.params;
  const body = await request.json().catch(() => ({}));
  const text = typeof body.message === "string" ? body.message : "";
  if (!text.trim()) return NextResponse.json({ error: "message_required" }, { status: 400 });

  try {
    const message = await addUserMessage(threadId, text);
    const memoryProposal = text.includes("记住") || text.includes("以后默认")
      ? await createMemoryProposal(threadId, text)
      : null;
    const run = memoryProposal ? null : await createRun(threadId);
    const agentCore = run ? await runAgentCore(threadId, run.id, text) : null;
    return NextResponse.json(
      {
        message,
        run,
        memoryProposal,
        agentCore,
        eventsUrl: run ? `/api/runs/${run.id}/events` : null,
      },
      { status: 202 },
    );
  } catch (error) {
    return jsonError(error);
  }
}

async function runAgentCore(threadId: string, runId: string, message: string) {
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
