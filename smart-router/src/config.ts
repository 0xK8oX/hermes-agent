/**
 * Smart Router - Config loading
 *
 * Resolves API keys from Wrangler secrets.
 * Plan configurations are fetched from the D1 database at runtime.
 */

import type { ProviderConfig } from "./types";
import { getPlan as dbGetPlan } from "./db";

/**
 * Get a plan by name from D1.
 * Falls back to "default" if not found.
 */
export async function getPlan(
  env: Env,
  name: string
): Promise<{ providers: ProviderConfig[] } | null> {
  let plan = await dbGetPlan(env.DB, name);
  if (!plan) {
    plan = await dbGetPlan(env.DB, "default");
  }
  return plan;
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
