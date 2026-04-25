/**
 * Smart Router - D1 Database Layer
 *
 * Plan storage with SQL schema:
 *   plans          -> plan slug
 *   plan_providers -> provider config per plan, ordered by priority
 */

import type { PlanConfig, ProviderConfig } from "./types";
import plansJson from "../plans.json";

export async function initDb(db: D1Database): Promise<void> {
  // Use prepared statements for DDL — D1 local handles these more reliably than exec()
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
