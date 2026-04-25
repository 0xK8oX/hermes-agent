/**
 * Smart Router - Config loading
 *
 * Resolves API keys from Wrangler secrets.
 * Plan configurations are fetched from the PlanStore Durable Object at runtime.
 */

import type { ProviderConfig } from "./types";

/**
 * Get a plan by name from the PlanStore DO.
 * Falls back to "default" if not found.
 */
export async function getPlan(
  env: Env,
  name: string
): Promise<{ providers: ProviderConfig[] } | null> {
  const id = env.PLAN_STORE.idFromName("global");
  const stub = env.PLAN_STORE.get(id);

  try {
    const res = await stub.fetch(
      `https://fake-host/plans/${encodeURIComponent(name)}`,
      { method: "GET" }
    );
    if (res.ok) {
      return (await res.json()) as { providers: ProviderConfig[] };
    }
  } catch {
    // fall through
  }

  // Fallback to "default"
  try {
    const res = await stub.fetch(
      `https://fake-host/plans/default`,
      { method: "GET" }
    );
    if (res.ok) {
      return (await res.json()) as { providers: ProviderConfig[] };
    }
  } catch {
    // fall through
  }

  return null;
}

/**
 * Resolve the API key for a provider from Wrangler secrets.
 * Secret name pattern: PROVIDER_KEY_<UPPERCASE_PROVIDER_NAME>
 */
export function getProviderKey(providerName: string, env: Env): string | null {
  const secretName = `PROVIDER_KEY_${providerName.toUpperCase().replace(/[^A-Z0-9]/g, "_")}`;
  const key = (env as unknown as Record<string, unknown>)[secretName];
  if (typeof key === "string" && key.length > 0) {
    return key;
  }
  return null;
}

/**
 * Return a sorted list of provider names from a plan.
 */
export async function getProviderNames(
  env: Env,
  planName: string
): Promise<string[]> {
  const plan = await getPlan(env, planName);
  if (!plan) return [];
  return plan.providers.map((p) => p.name);
}

/**
 * Get provider config by name within a plan.
 */
export async function getProviderConfig(
  env: Env,
  planName: string,
  providerName: string
): Promise<ProviderConfig | null> {
  const plan = await getPlan(env, planName);
  if (!plan) return null;
  return plan.providers.find((p) => p.name === providerName) || null;
}
