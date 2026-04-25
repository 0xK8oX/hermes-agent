/**
 * Smart Router - Config loading
 *
 * Plan configurations and encrypted API keys are fetched from D1 at runtime.
 * Master encryption key (KEY_ENCRYPTION_KEY) stays in Wrangler secrets.
 */

import type { ProviderConfig } from "./types";
import { getPlan as dbGetPlan, getEncryptedKey } from "./db";
import { decryptKey } from "./crypto";

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
 * Resolve the API key for a provider from D1 (decrypted on-the-fly).
 */
export async function getProviderKey(
  providerName: string,
  env: Env
): Promise<string | null> {
  const encrypted = await getEncryptedKey(env.DB, providerName.toLowerCase());
  if (!encrypted) return null;
  try {
    return await decryptKey(encrypted, env);
  } catch {
    return null;
  }
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
