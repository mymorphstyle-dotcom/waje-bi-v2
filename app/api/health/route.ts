import { spawn } from "child_process";
import { Pool } from "pg";
import { wajePythonInvocation } from "../_pythonRuntime";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type HealthCheck = {
  name: string;
  status: "ok" | "failed";
  detail: string;
};

const CLICKHOUSE_ENV = [
  "WAJE_CLICKHOUSE_HOST",
  "WAJE_CLICKHOUSE_PORT",
  "WAJE_CLICKHOUSE_USER",
  "WAJE_CLICKHOUSE_PASSWORD",
  "WAJE_CLICKHOUSE_DATABASE",
  "WAJE_CLICKHOUSE_SECURE",
];

export async function GET() {
  const checks = await Promise.all([
    gatewayHealth(),
    postgresHealth(),
    llmHealth(),
    pythonHealth("python_bi_agent_core", "import bi_agent.conversation.agent_core"),
    pythonHealth(
      "langgraph_adapter",
      "from bi_agent.runtime.langgraph_workflow import build_single_authority_graph; build_single_authority_graph()",
    ),
    clickhouseHealth(),
  ]);
  return Response.json({
    status: checks.every((check) => check.status === "ok") ? "ok" : "degraded",
    checks,
  });
}

function gatewayHealth(): HealthCheck {
  return { name: "frontend_gateway", status: "ok", detail: "route_responded" };
}

function llmHealth(): HealthCheck {
  const missing = [];
  if (!process.env.WAJE_LLM_MODEL) missing.push("WAJE_LLM_MODEL");
  if (
    !process.env.WAJE_LLM_API_KEY &&
    !process.env.OPENAI_API_KEY &&
    !process.env.DEEPSEEK_API_KEY
  ) {
    missing.push("WAJE_LLM_API_KEY|OPENAI_API_KEY|DEEPSEEK_API_KEY");
  }
  if (missing.length) {
    return { name: "llm_access", status: "failed", detail: `missing_env:${missing.join(",")}` };
  }
  return { name: "llm_access", status: "ok", detail: "model_and_key_configured" };
}

async function postgresHealth(): Promise<HealthCheck> {
  const connectionString = process.env.WAJE_RUNTIME_DATABASE_URL || process.env.DATABASE_URL;
  if (!connectionString) {
    return {
      name: "postgres_runtime_store",
      status: "failed",
      detail: "missing_database_url",
    };
  }
  const pool = new Pool({ connectionString, max: 1 });
  try {
    await pool.query("SELECT 1");
    return { name: "postgres_runtime_store", status: "ok", detail: "select_1_passed" };
  } catch {
    return { name: "postgres_runtime_store", status: "failed", detail: "select_1_failed" };
  } finally {
    await pool.end().catch(() => undefined);
  }
}

async function clickhouseHealth(): Promise<HealthCheck> {
  const missing = CLICKHOUSE_ENV.filter((name) => !process.env[name]);
  if (missing.length) {
    return {
      name: "clickhouse_access",
      status: "failed",
      detail: `missing_env:${missing.join(",")}`,
    };
  }
  return pythonHealth(
    "clickhouse_access",
    [
      "from bi_agent.runtime.clickhouse_runtime import ClickHouseRuntime",
      "runtime = ClickHouseRuntime.from_env()",
      "result = runtime.show_tables()",
      "raise SystemExit(0 if result.ok else 1)",
    ].join("; "),
  );
}

function pythonHealth(name: string, code: string, timeoutMs = 5000): Promise<HealthCheck> {
  return new Promise((resolve) => {
    const invocation = wajePythonInvocation(["-c", code]);
    const child = spawn(invocation.command, invocation.args, {
      cwd: process.cwd(),
      env: process.env,
    });
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill();
      resolve({ name, status: "failed", detail: "timeout" });
    }, timeoutMs);
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("error", () => {
      clearTimeout(timer);
      resolve({ name, status: "failed", detail: "spawn_failed" });
    });
    child.on("close", (codeNumber) => {
      clearTimeout(timer);
      resolve({
        name,
        status: codeNumber === 0 ? "ok" : "failed",
        detail: codeNumber === 0 ? "python_check_passed" : stderr.trim() || "python_check_failed",
      });
    });
  });
}
