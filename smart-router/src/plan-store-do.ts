/**
 * Smart Router - PlanStore Durable Object
 *
 * Persists plan configurations dynamically.
 * Seeds from bundled plans.json on first creation.
 */

import type { PlansConfig, PlanConfig } from "./types";

const STORAGE_KEY = "plan_store";
const SEEDED_KEY = "plan_store_seeded";

// plans.json is bundled at build time
import defaultPlans from "../plans.json";

export class PlanStore implements DurableObject {
  private state: DurableObjectState;
  private plans: Map<string, PlanConfig> = new Map();
  private loaded: boolean = false;

  constructor(state: DurableObjectState) {
    this.state = state;
  }

  private async loadState(): Promise<void> {
    if (this.loaded) return;

    // Seed from bundled plans.json on first ever access
    const seeded = await this.state.storage.get<boolean>(SEEDED_KEY);
    if (!seeded) {
      const plans = (defaultPlans as PlansConfig).plans;
      for (const [slug, config] of Object.entries(plans)) {
        this.plans.set(slug, config);
      }
      await this.saveState();
      await this.state.storage.put(SEEDED_KEY, true);
    } else {
      const stored = await this.state.storage.get<Record<string, PlanConfig>>(STORAGE_KEY);
      if (stored) {
        for (const [slug, config] of Object.entries(stored)) {
          this.plans.set(slug, config);
        }
      }
    }

    this.loaded = true;
  }

  private async saveState(): Promise<void> {
    const obj: Record<string, PlanConfig> = {};
    for (const [slug, config] of this.plans.entries()) {
      obj[slug] = config;
    }
    await this.state.storage.put(STORAGE_KEY, obj);
  }

  async fetch(request: Request): Promise<Response> {
    await this.loadState();
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/plans" && request.method === "GET") {
      const obj: Record<string, PlanConfig> = {};
      for (const [slug, config] of this.plans.entries()) {
        obj[slug] = config;
      }
      return new Response(JSON.stringify(obj), {
        headers: { "Content-Type": "application/json" },
      });
    }

    if (path === "/plans" && request.method === "POST") {
      const body = (await request.json()) as { slug: string; config: PlanConfig };
      if (!body.slug || !body.config || !Array.isArray(body.config.providers)) {
        return new Response(JSON.stringify({ error: "Missing slug or config.providers" }), {
          status: 400,
          headers: { "Content-Type": "application/json" },
        });
      }
      this.plans.set(body.slug, body.config);
      await this.saveState();
      return new Response(JSON.stringify({ ok: true, slug: body.slug }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    const planMatch = path.match(/^\/plans\/([^/]+)$/);
    if (planMatch) {
      const slug = decodeURIComponent(planMatch[1]);

      if (request.method === "GET") {
        const config = this.plans.get(slug);
        if (!config) {
          return new Response(JSON.stringify({ error: "Plan not found" }), {
            status: 404,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify(config), {
          headers: { "Content-Type": "application/json" },
        });
      }

      if (request.method === "PUT") {
        const body = (await request.json()) as PlanConfig;
        if (!body.providers || !Array.isArray(body.providers)) {
          return new Response(JSON.stringify({ error: "Missing providers array" }), {
            status: 400,
            headers: { "Content-Type": "application/json" },
          });
        }
        this.plans.set(slug, body);
        await this.saveState();
        return new Response(JSON.stringify({ ok: true, slug }), {
          headers: { "Content-Type": "application/json" },
        });
      }

      if (request.method === "DELETE") {
        this.plans.delete(slug);
        await this.saveState();
        return new Response(JSON.stringify({ ok: true, slug }), {
          headers: { "Content-Type": "application/json" },
        });
      }
    }

    return new Response(JSON.stringify({ error: "Not found" }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    });
  }
}
