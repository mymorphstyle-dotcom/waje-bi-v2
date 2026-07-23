import { timingSafeEqual } from "node:crypto";
import { accessSync, constants } from "node:fs";
import { isAbsolute } from "node:path";
import { Pool } from "pg";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type HealthCheck = {
  name: string;
  status: "ok" | "failed";
  detail: string;
};

const READINESS_HEADER = "x-waje-readiness-token";
const POSTGRES_TIMEOUT_MS = 2_000;

export async function GET(request: Request) {
  const mode = new URL(request.url).searchParams.get("mode") ?? "liveness";
  if (mode === "liveness") {
    return Response.json({
      status: "ok",
      checks: [gatewayHealth()],
    });
  }
  if (mode !== "readiness") {
    return Response.json({ error: "health_mode_invalid" }, { status: 400 });
  }
  if (!readinessAuthorized(request)) {
    return Response.json({ error: "health_readiness_unavailable" }, { status: 404 });
  }

  const checks = await Promise.all([
    postgresHealth(),
    runtimeConfigurationHealth(),
  ]);
  const ready = checks.every((check) => check.status === "ok");
  return Response.json(
    { status: ready ? "ok" : "degraded", checks },
    { status: ready ? 200 : 503 },
  );
}

function gatewayHealth(): HealthCheck {
  return { name: "frontend_gateway", status: "ok", detail: "route_responded" };
}

function readinessAuthorized(request: Request) {
  if (process.env.NODE_ENV !== "production") return true;
  const expected = process.env.WAJE_HEALTH_READINESS_TOKEN ?? "";
  const supplied = request.headers.get(READINESS_HEADER) ?? "";
  if (Buffer.byteLength(expected, "utf8") < 32) return false;
  const expectedBytes = Buffer.from(expected, "utf8");
  const suppliedBytes = Buffer.from(supplied, "utf8");
  return suppliedBytes.length === expectedBytes.length
    && timingSafeEqual(suppliedBytes, expectedBytes);
}

function runtimeConfigurationHealth(): HealthCheck {
  const pythonExecutable = process.env.WAJE_PYTHON_EXECUTABLE ?? "";
  let pythonAvailable = false;
  if (isAbsolute(pythonExecutable) && !pythonExecutable.includes("\0")) {
    try {
      accessSync(pythonExecutable, constants.X_OK);
      pythonAvailable = true;
    } catch {
      pythonAvailable = false;
    }
  }
  const configured = Boolean(
    process.env.WAJE_LLM_PROVIDER
      && process.env.WAJE_LLM_BASE_URL
      && process.env.WAJE_LLM_MODEL
      && (process.env.WAJE_LLM_API_KEY || process.env.DEEPSEEK_API_KEY)
      && (process.env.WAJE_CLICKHOUSE_HOST || process.env.WAJE_CLICKHOUSE_URL)
      && pythonAvailable,
  );
  return configured
    ? {
        name: "runtime_configuration",
        status: "ok",
        detail: "required_configuration_present",
      }
    : {
        name: "runtime_configuration",
        status: "failed",
        detail: "required_configuration_incomplete",
      };
}

async function postgresHealth(): Promise<HealthCheck> {
  const connectionString = process.env.WAJE_RUNTIME_DATABASE_URL
    || process.env.DATABASE_URL;
  if (!connectionString) {
    return {
      name: "postgres_runtime_store",
      status: "failed",
      detail: "configuration_incomplete",
    };
  }
  const pool = new Pool({
    connectionString,
    max: 1,
    connectionTimeoutMillis: POSTGRES_TIMEOUT_MS,
    statement_timeout: POSTGRES_TIMEOUT_MS,
  });
  try {
    await pool.query("SELECT 1");
    return {
      name: "postgres_runtime_store",
      status: "ok",
      detail: "connection_verified",
    };
  } catch {
    return {
      name: "postgres_runtime_store",
      status: "failed",
      detail: "connection_unavailable",
    };
  } finally {
    await pool.end().catch(() => undefined);
  }
}
