/**
 * Smart Router - Cloudflare Worker Entry Point
 *
 * Endpoints:
 *   POST /v1/chat/completions  (OpenAI format)
 *   POST /v1/messages          (Anthropic format)
 *   GET  /v1/health            (Provider health status)
 */

import { HealthTracker } from "./health-do";
import { routeRequest } from "./router";
import type { ClientFormat } from "./types";

export { HealthTracker };

function corsHeaders(): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Plan",
  };
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    const url = new URL(request.url);
    const path = url.pathname;

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
