/**
 * Smart Router - Config loading
 *
 * Loads provider pool definitions from bundled plans.json
 * and resolves API keys from Wrangler secrets.
 */

import type { PlansConfig, ProviderConfig } from "./types";

// plans.json is bundled at build time via import
import plansJson from "../plans.json";

const plans: PlansConfig = plansJson as PlansConfig;

/**
 * Get a plan by name. Falls back to "default" if not found.
 */
export function getPlan(name: string): { providers: ProviderConfig[] } | null {
  const plan = plans.plans[name] || plans.plans["default"];
  if (!plan) {
    return null;
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
export function getProviderNames(planName: string): string[] {
  const plan = getPlan(planName);
  if (!plan) return [];
  return plan.providers.map((p) => p.name);
}

/**
 * Get provider config by name within a plan.
 */
export function getProviderConfig(
  planName: string,
  providerName: string
): ProviderConfig | null {
  const plan = getPlan(planName);
  if (!plan) return null;
  return plan.providers.find((p) => p.name === providerName) || null;
}
