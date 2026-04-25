/**
 * Smart Router - Cloudflare Worker Entry Point
 *
 * Endpoints:
 *   POST /v1/chat/completions  (OpenAI format)
 *   POST /v1/messages          (Anthropic format)
 *   GET  /v1/health            (Provider health status)
 *
 * Plan Management:
 *   GET    /v1/plans           (List all plans)
 *   POST   /v1/plans           (Create a new plan)
 *   GET    /v1/plans/:slug     (Get a specific plan)
 *   PUT    /v1/plans/:slug     (Update a plan)
 *   DELETE /v1/plans/:slug     (Delete a plan)
 *
 * Key Management:
 *   GET    /v1/keys            (List provider names that have keys)
 *   POST   /v1/keys            (Store an encrypted API key)
 *   DELETE /v1/keys/:provider  (Delete a key)
 */

import { HealthTracker } from "./health-do";
import { routeRequest } from "./router";
import type { ClientFormat } from "./types";
import {
  initDb,
  seedPlansIfEmpty,
  listPlans,
  getPlan,
  upsertPlan,
  deletePlan,
  setEncryptedKey,
  deleteKey,
  listKeys,
  migrateKeysFromEnv,
} from "./db";
import { encryptKey } from "./crypto";

export { HealthTracker };

function corsHeaders(): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Plan",
  };
}

let dbInitialized = false;

async function ensureDb(env: Env): Promise<void> {
  if (dbInitialized) return;
  await initDb(env.DB);
  await seedPlansIfEmpty(env.DB);
  await migrateKeysFromEnv(env.DB, env, encryptKey);
  dbInitialized = true;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    await ensureDb(env);

    const url = new URL(request.url);
    const path = url.pathname;

    // ── Plan Management API ──────────────────────────────────────────────
    if (path === "/v1/plans") {
      if (request.method === "GET") {
        return handleListPlans(env);
      }
      if (request.method === "POST") {
        return handleCreatePlan(request, env);
      }
    }

    const planMatch = path.match(/^\/v1\/plans\/([^/]+)$/);
    if (planMatch) {
      const slug = decodeURIComponent(planMatch[1]);
      if (request.method === "GET") {
        return handleGetPlan(slug, env);
      }
      if (request.method === "PUT") {
        return handleUpdatePlan(slug, request, env);
      }
      if (request.method === "DELETE") {
        return handleDeletePlan(slug, env);
      }
    }

    // ── Key Management API ───────────────────────────────────────────────
    if (path === "/v1/keys") {
      if (request.method === "GET") {
        return handleListKeys(env);
      }
      if (request.method === "POST") {
        return handleStoreKey(request, env);
      }
    }

    const keyMatch = path.match(/^\/v1\/keys\/([^/]+)$/);
    if (keyMatch && request.method === "DELETE") {
      return handleDeleteKey(decodeURIComponent(keyMatch[1]), env);
    }

    // ── Health check endpoint ────────────────────────────────────────────
    if (path === "/v1/health" && request.method === "GET") {
      const plan = url.searchParams.get("plan") || "default";
      const id = env.HEALTH_TRACKER.idFromName("global");
      const stub = env.HEALTH_TRACKER.get(id);
      const res = await stub.fetch(`https://fake-host/health/state?plan=${encodeURIComponent(plan)}`);
      return new Response(res.body, {
        status: res.status,
        headers: { ...corsHeaders(), "Content-Type": "application/json" },
      });
    }

    // ── OpenAI Chat Completions ──────────────────────────────────────────
    if (path === "/v1/chat/completions" && request.method === "POST") {
      return handleChatRequest(request, env, "openai");
    }

    // ── Anthropic Messages ───────────────────────────────────────────────
    if (path === "/v1/messages" && request.method === "POST") {
      return handleChatRequest(request, env, "anthropic");
    }

    return new Response(JSON.stringify({ error: "Not found" }), {
      status: 404,
      headers: { ...corsHeaders(), "Content-Type": "application/json" },
    });
  },
};

// ── Plan Management Handlers ─────────────────────────────────────────────

async function handleListPlans(env: Env): Promise<Response> {
  const plans = await listPlans(env.DB);
  return new Response(JSON.stringify(plans), {
    headers: { ...corsHeaders(), "Content-Type": "application/json" },
  });
}

async function handleCreatePlan(request: Request, env: Env): Promise<Response> {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
      status: 400,
      headers: { ...corsHeaders(), "Content-Type": "application/json" },
    });
  }

  const slug = (body as Record<string, unknown>)?.slug as string | undefined;
  const config = (body as Record<string, unknown>)?.config as
    | { providers: unknown[] }
    | undefined;

  if (!slug || !config || !Array.isArray(config.providers)) {
    return new Response(
      JSON.stringify({ error: "Missing slug or config.providers" }),
      { status: 400, headers: { ...corsHeaders(), "Content-Type": "application/json" } }
    );
  }

  await upsertPlan(env.DB, slug, config as unknown as import("./types").PlanConfig);

  return new Response(JSON.stringify({ ok: true, slug }), {
    headers: { ...corsHeaders(), "Content-Type": "application/json" },
  });
}

async function handleGetPlan(slug: string, env: Env): Promise<Response> {
  const config = await getPlan(env.DB, slug);
  if (!config) {
    return new Response(JSON.stringify({ error: "Plan not found" }), {
      status: 404,
      headers: { ...corsHeaders(), "Content-Type": "application/json" },
    });
  }
  return new Response(JSON.stringify(config), {
    headers: { ...corsHeaders(), "Content-Type": "application/json" },
  });
}

async function handleUpdatePlan(
  slug: string,
  request: Request,
  env: Env
): Promise<Response> {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
      status: 400,
      headers: { ...corsHeaders(), "Content-Type": "application/json" },
    });
  }

  const config = body as { providers?: unknown[] };
  if (!config.providers || !Array.isArray(config.providers)) {
    return new Response(
      JSON.stringify({ error: "Missing providers array" }),
      { status: 400, headers: { ...corsHeaders(), "Content-Type": "application/json" } }
    );
  }

  await upsertPlan(env.DB, slug, config as unknown as import("./types").PlanConfig);

  return new Response(JSON.stringify({ ok: true, slug }), {
    headers: { ...corsHeaders(), "Content-Type": "application/json" },
  });
}

async function handleDeletePlan(slug: string, env: Env): Promise<Response> {
  await deletePlan(env.DB, slug);
  return new Response(JSON.stringify({ ok: true, slug }), {
    headers: { ...corsHeaders(), "Content-Type": "application/json" },
  });
}

// ── Key Management Handlers ──────────────────────────────────────────────

async function handleListKeys(env: Env): Promise<Response> {
  const names = await listKeys(env.DB);
  return new Response(JSON.stringify({ keys: names }), {
    headers: { ...corsHeaders(), "Content-Type": "application/json" },
  });
}

async function handleStoreKey(request: Request, env: Env): Promise<Response> {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
      status: 400,
      headers: { ...corsHeaders(), "Content-Type": "application/json" },
    });
  }

  const providerName = (body as Record<string, unknown>)?.provider_name as string | undefined;
  const apiKey = (body as Record<string, unknown>)?.api_key as string | undefined;

  if (!providerName || !apiKey) {
    return new Response(
      JSON.stringify({ error: "Missing provider_name or api_key" }),
      { status: 400, headers: { ...corsHeaders(), "Content-Type": "application/json" } }
    );
  }

  try {
    const encrypted = await encryptKey(apiKey, env);
    await setEncryptedKey(env.DB, providerName.toLowerCase(), encrypted);
    return new Response(JSON.stringify({ ok: true, provider_name: providerName }), {
      headers: { ...corsHeaders(), "Content-Type": "application/json" },
    });
  } catch (err) {
    return new Response(
      JSON.stringify({ error: `Encryption failed: ${err instanceof Error ? err.message : String(err)}` }),
      { status: 500, headers: { ...corsHeaders(), "Content-Type": "application/json" } }
    );
  }
}

async function handleDeleteKey(providerName: string, env: Env): Promise<Response> {
  await deleteKey(env.DB, providerName.toLowerCase());
  return new Response(JSON.stringify({ ok: true, provider_name: providerName }), {
    headers: { ...corsHeaders(), "Content-Type": "application/json" },
  });
}

// ── Chat Request Handler ─────────────────────────────────────────────────

async function handleChatRequest(
  request: Request,
  env: Env,
  clientFormat: ClientFormat
): Promise<Response> {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
      status: 400,
      headers: { ...corsHeaders(), "Content-Type": "application/json" },
    });
  }

  const plan = request.headers.get("X-Plan") || "default";
  const isStreaming = (body as Record<string, unknown>)?.stream === true;

  const routerReq = {
    body,
    clientFormat,
    plan,
    isStreaming,
  };

  const response = await routeRequest(routerReq, env);

  // Add CORS headers to the response
  const newHeaders = new Headers(response.headers);
  for (const [k, v] of Object.entries(corsHeaders())) {
    newHeaders.set(k, v);
  }

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: newHeaders,
  });
}
