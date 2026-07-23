import { gatewayError } from "./_conversationStore";

type SseBudgetState = {
  total: number;
  byActor: Map<string, number>;
};

const globalBudget = globalThis as typeof globalThis & {
  __wajeSseBudget?: SseBudgetState;
};

export type CustomerSseLease = {
  expiresAt: number;
  release: () => void;
};

export function acquireCustomerSseLease(actorId: string): CustomerSseLease {
  const state = globalBudget.__wajeSseBudget ??= {
    total: 0,
    byActor: new Map(),
  };
  const globalLimit = boundedIntegerEnv("WAJE_SSE_MAX_CONNECTIONS", 128, 1, 4096);
  const actorLimit = boundedIntegerEnv("WAJE_SSE_MAX_CONNECTIONS_PER_ACTOR", 4, 1, 32);
  const actorCount = state.byActor.get(actorId) ?? 0;
  if (state.total >= globalLimit || actorCount >= actorLimit) {
    throw gatewayError("customer_stream_capacity_exceeded");
  }
  state.total += 1;
  state.byActor.set(actorId, actorCount + 1);
  let released = false;
  return {
    expiresAt: Date.now() + boundedIntegerEnv(
      "WAJE_SSE_CONNECTION_TTL_MS",
      5 * 60 * 1000,
      10_000,
      30 * 60 * 1000,
    ),
    release() {
      if (released) return;
      released = true;
      state.total = Math.max(0, state.total - 1);
      const nextActorCount = Math.max(0, (state.byActor.get(actorId) ?? 1) - 1);
      if (nextActorCount === 0) state.byActor.delete(actorId);
      else state.byActor.set(actorId, nextActorCount);
    },
  };
}

export function customerSsePollIntervalMs() {
  return boundedIntegerEnv("WAJE_SSE_POLL_INTERVAL_MS", 2_000, 500, 30_000);
}

function boundedIntegerEnv(
  name: string,
  fallback: number,
  minimum: number,
  maximum: number,
) {
  const raw = process.env[name];
  if (raw === undefined) return fallback;
  if (!/^\d+$/.test(raw)) throw gatewayError("sse_budget_configuration_invalid");
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw gatewayError("sse_budget_configuration_invalid");
  }
  return value;
}
