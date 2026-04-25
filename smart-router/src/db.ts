/**
 * Smart Router - D1 Database Layer
 *
 * Plan storage with SQL schema:
 *   plans          -> plan slug
 *   plan_providers -> provider config per plan, ordered
 *   provider_keys  -> encrypted API keys per provider
 */

import type { PlanConfig, ProviderConfig } from "./types";
import plansJson from "../plans.json";

export async function initDb(db: D1Database): Promise<void> {
  await db.prepare(
    "CREATE TABLE IF NOT EXISTS plans (slug TEXT PRIMARY KEY)"
  ).run();

  await db.prepare(
    "CREATE TABLE IF NOT EXISTS plan_providers (" +
    "id INTEGER PRIMARY KEY AUTOINCREMENT, " +
    "plan_slug TEXT NOT NULL, " +
    "name TEXT NOT NULL, " +
    "base_url TEXT NOT NULL, " +
    "model TEXT NOT NULL, " +
    "format TEXT NOT NULL, " +
    "timeout INTEGER DEFAULT 60, " +
    "priority INTEGER DEFAULT 0)"
  ).run();

  await db.prepare(
    "CREATE TABLE IF NOT EXISTS provider_keys (" +
    "provider_name TEXT PRIMARY KEY, " +
    "encrypted_key TEXT NOT NULL)"
  ).run();

  await db.prepare(
    "CREATE INDEX IF NOT EXISTS idx_plan_providers_plan ON plan_providers(plan_slug)"
  ).run();
}

export async function seedPlansIfEmpty(db: D1Database): Promise<void> {
  const row = await db.prepare("SELECT COUNT(*) as count FROM plans").first();
  if (row && (row.count as number) > 0) return;

  const plans = (plansJson as { plans: Record<string, PlanConfig> }).plans;
  for (const [slug, config] of Object.entries(plans)) {
    await upsertPlan(db, slug, config);
  }
}

export async function listPlans(db: D1Database): Promise<Record<string, PlanConfig>> {
  const rows = await db.prepare("SELECT slug FROM plans ORDER BY slug").all();
  const result: Record<string, PlanConfig> = {};

  for (const row of (rows.results ?? []) as Array<{ slug: string }>) {
    const config = await getPlan(db, row.slug);
    if (config) result[row.slug] = config;
  }
  return result;
}

export async function getPlan(
  db: D1Database,
  slug: string
): Promise<PlanConfig | null> {
  const rows = await db
    .prepare(
      "SELECT name, base_url, model, format, timeout " +
      "FROM plan_providers WHERE plan_slug = ? ORDER BY priority"
    )
    .bind(slug)
    .all();

  if (!rows.results || rows.results.length === 0) {
    return null;
  }

  return {
    providers: rows.results as unknown as ProviderConfig[],
  };
}

export async function upsertPlan(
  db: D1Database,
  slug: string,
  config: PlanConfig
): Promise<void> {
  await db
    .prepare("INSERT OR REPLACE INTO plans (slug) VALUES (?)")
    .bind(slug)
    .run();

  await db.prepare("DELETE FROM plan_providers WHERE plan_slug = ?").bind(slug).run();

  const stmt = db.prepare(
    "INSERT INTO plan_providers " +
    "(plan_slug, name, base_url, model, format, timeout, priority) " +
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
  );

  for (let i = 0; i < config.providers.length; i++) {
    const p = config.providers[i];
    await stmt.bind(slug, p.name, p.base_url, p.model, p.format, p.timeout ?? 60, i).run();
  }
}

export async function deletePlan(db: D1Database, slug: string): Promise<void> {
  await db.prepare("DELETE FROM plan_providers WHERE plan_slug = ?").bind(slug).run();
  await db.prepare("DELETE FROM plans WHERE slug = ?").bind(slug).run();
}

// ── Provider Key Management ──────────────────────────────────────────────

export async function getEncryptedKey(
  db: D1Database,
  providerName: string
): Promise<string | null> {
  const row = await db
    .prepare("SELECT encrypted_key FROM provider_keys WHERE provider_name = ?")
    .bind(providerName)
    .first();
  return row ? (row.encrypted_key as string) : null;
}

export async function setEncryptedKey(
  db: D1Database,
  providerName: string,
  encryptedKey: string
): Promise<void> {
  await db
    .prepare("INSERT OR REPLACE INTO provider_keys (provider_name, encrypted_key) VALUES (?, ?)")
    .bind(providerName, encryptedKey)
    .run();
}

export async function deleteKey(db: D1Database, providerName: string): Promise<void> {
  await db
    .prepare("DELETE FROM provider_keys WHERE provider_name = ?")
    .bind(providerName)
    .run();
}

export async function listKeys(db: D1Database): Promise<string[]> {
  const rows = await db.prepare("SELECT provider_name FROM provider_keys ORDER BY provider_name").all();
  return (rows.results ?? []).map((r: unknown) => (r as { provider_name: string }).provider_name);
}

/**
 * One-time migration: encrypt keys from Wrangler secrets and store in D1.
 * Only runs if provider_keys table is empty and env vars exist.
 */
export async function migrateKeysFromEnv(
  db: D1Database,
  env: Env,
  encryptFn: (plaintext: string, env: Env) => Promise<string> | string
): Promise<void> {
  const row = await db.prepare("SELECT COUNT(*) as count FROM provider_keys").first();
  if (row && (row.count as number) > 0) return;

  const envVars = env as unknown as Record<string, unknown>;
  for (const [key, value] of Object.entries(envVars)) {
    if (key.startsWith("PROVIDER_KEY_") && typeof value === "string" && value.length > 0) {
      const providerName = key
        .replace("PROVIDER_KEY_", "")
        .replace(/_/g, "-")
        .toLowerCase();
      const encrypted = await encryptFn(value, env);
      await setEncryptedKey(db, providerName, encrypted);
    }
  }
}
