/**
 * Smart Router - HealthTracker Durable Object
 *
 * Circuit breaker with per-provider health state.
 * State is persisted via DO storage to survive hibernation.
 */

import type { ProviderConfig, ProviderHealth } from "./types";

interface HealthState {
  providers: Record<string, ProviderHealth>;
}

const STORAGE_KEY = "health_state";

const CIRCUIT_RULES: Record<string, { threshold: number; cooldownMs: number }> = {
  quota: { threshold: 1, cooldownMs: 5 * 60 * 60 * 1000 },      // 5 hours
  rate_limit: { threshold: 3, cooldownMs: 5 * 60 * 1000 },       // 5 min
  server_error: { threshold: 2, cooldownMs: 2 * 60 * 1000 },     // 2 min
  connection: { threshold: 2, cooldownMs: 60 * 1000 },           // 1 min
  timeout: { threshold: 2, cooldownMs: 2 * 60 * 1000 },          // 2 min
  unknown: { threshold: 3, cooldownMs: 60 * 1000 },              // 1 min
};

function classifyFailure(status: number, message: string): string {
  const msg = message.toLowerCase();
  if (status === 402 || msg.includes("quota") || msg.includes("credit") || msg.includes("billing")) {
    return "quota";
  }
  if (status === 429 || msg.includes("rate limit") || msg.includes("too many requests")) {
    return "rate_limit";
  }
  if (status >= 500 && status < 600) {
    return "server_error";
  }
  if (msg.includes("connection") || msg.includes("refused") || msg.includes("econnrefused")) {
    return "connection";
  }
  if (msg.includes("timeout") || msg.includes("etimedout")) {
    return "timeout";
  }
  return "unknown";
}

function makeHealthy(): ProviderHealth {
  return {
    status: "healthy",
    consecutiveFailures: 0,
    lastFailureAt: 0,
    cooldownUntil: 0,
    lastFailureReason: "",
  };
}

export class HealthTracker implements DurableObject {
  private state: DurableObjectState;
  private health: Map<string, HealthState> = new Map();
  private loaded: boolean = false;

  constructor(state: DurableObjectState) {
    this.state = state;
  }

  private async loadState(): Promise<void> {
    if (this.loaded) return;
    const stored = await this.state.storage.get<Record<string, HealthState>>(STORAGE_KEY);
    if (stored) {
      for (const [plan, hs] of Object.entries(stored)) {
        this.health.set(plan, hs);
      }
    }
    this.loaded = true;
  }

  private async saveState(): Promise<void> {
    const obj: Record<string, HealthState> = {};
    for (const [plan, hs] of this.health.entries()) {
      obj[plan] = hs;
    }
    await this.state.storage.put(STORAGE_KEY, obj);
  }

  async fetch(request: Request): Promise<Response> {
    await this.loadState();
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/health" && request.method === "POST") {
      const body = (await request.json()) as {
        action: "recordFailure" | "recordSuccess" | "getHealthyProviders";
        plan: string;
        provider?: string;
        status?: number;
        message?: string;
        providerList?: ProviderConfig[];
      };

      switch (body.action) {
        case "recordFailure":
          if (!body.provider) {
            return new Response("Missing provider", { status: 400 });
          }
          this.recordFailure(body.plan, body.provider, body.status ?? 0, body.message ?? "");
          await this.saveState();
          return new Response(JSON.stringify({ ok: true }), {
            headers: { "Content-Type": "application/json" },
          });

        case "recordSuccess":
          if (!body.provider) {
            return new Response("Missing provider", { status: 400 });
          }
          this.recordSuccess(body.plan, body.provider);
          await this.saveState();
          return new Response(JSON.stringify({ ok: true }), {
            headers: { "Content-Type": "application/json" },
          });

        case "getHealthyProviders":
          if (!body.providerList) {
            return new Response("Missing providerList", { status: 400 });
          }
          const healthy = this.getHealthyProviders(body.plan, body.providerList);
          return new Response(JSON.stringify({ providers: healthy }), {
            headers: { "Content-Type": "application/json" },
          });

        default:
          return new Response("Unknown action", { status: 400 });
      }
    }

    if (path === "/health/state" && request.method === "GET") {
      const plan = url.searchParams.get("plan") ?? "default";
      const state = this.health.get(plan);
      return new Response(JSON.stringify(state ?? { providers: {} }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response("Not found", { status: 404 });
  }

  private getPlanState(plan: string): HealthState {
    if (!this.health.has(plan)) {
      this.health.set(plan, { providers: {} });
    }
    return this.health.get(plan)!;
  }

  private getProviderHealth(plan: string, provider: string): ProviderHealth {
    const planState = this.getPlanState(plan);
    if (!planState.providers[provider]) {
      planState.providers[provider] = makeHealthy();
    }
    return planState.providers[provider];
  }

  recordFailure(plan: string, provider: string, status: number, message: string): void {
    const h = this.getProviderHealth(plan, provider);
    const reason = classifyFailure(status, message);
    const rule = CIRCUIT_RULES[reason] ?? CIRCUIT_RULES.unknown;

    h.consecutiveFailures++;
    h.lastFailureAt = Date.now();
    h.lastFailureReason = reason;

    if (h.consecutiveFailures >= rule.threshold) {
      h.status = "unhealthy";
      h.cooldownUntil = Date.now() + rule.cooldownMs;
      console.log(`[CIRCUIT_BREAKER] TRIPPED: plan=${plan} provider=${provider} reason=${reason} failures=${h.consecutiveFailures} cooldownMs=${rule.cooldownMs} status=${status}`);
    } else if (h.consecutiveFailures >= Math.max(1, Math.floor(rule.threshold / 2))) {
      h.status = "degraded";
      console.log(`[CIRCUIT_BREAKER] DEGRADED: plan=${plan} provider=${provider} reason=${reason} failures=${h.consecutiveFailures} status=${status}`);
    } else {
      console.log(`[CIRCUIT_BREAKER] RECORD_FAILURE: plan=${plan} provider=${provider} reason=${reason} failures=${h.consecutiveFailures} status=${status}`);
    }
  }

  recordSuccess(plan: string, provider: string): void {
    const h = this.getProviderHealth(plan, provider);
    const wasUnhealthy = h.status === "unhealthy" || h.status === "degraded";
    h.status = "healthy";
    h.consecutiveFailures = 0;
    h.cooldownUntil = 0;
    h.lastFailureReason = "";
    if (wasUnhealthy) {
      console.log(`[CIRCUIT_BREAKER] RECOVERED: plan=${plan} provider=${provider}`);
    }
  }

  getHealthyProviders(plan: string, providerList: ProviderConfig[]): ProviderConfig[] {
    const now = Date.now();
    const result: ProviderConfig[] = [];

    for (const p of providerList) {
      const h = this.getProviderHealth(plan, p.name);

      // If cooldown expired, auto-reset to healthy
      if (h.status === "unhealthy" && now >= h.cooldownUntil) {
        console.log(`[CIRCUIT_BREAKER] COOLDOWN_EXPIRED: plan=${plan} provider=${p.name} reset to healthy`);
        h.status = "healthy";
        h.consecutiveFailures = 0;
        h.cooldownUntil = 0;
      }

      if (h.status !== "unhealthy") {
        result.push(p);
      }
    }

    return result;
  }
}
