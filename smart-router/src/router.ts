/**
 * Smart Router - Core routing logic
 *
 * 1. Get healthy providers from HealthTracker DO
 * 2. Try each provider in priority order
 * 3. Translate request/response between formats
 * 4. On failure: report to DO, try next
 * 5. On success: return translated response
 */

import type { ClientFormat, ProviderConfig, RouterRequest, TranslatedRequest } from "./types";
import { getPlan, getProviderKey } from "./config";
import { callProvider } from "./providers/client";
import {
  translateRequestToProvider,
  translateResponseToClient,
} from "./translation";
import { parseSseEvents, serializeSseEvents, createAnthropicSseToOpenAiTranslator, createOpenAiSseToAnthropicTranslator } from "./translation/streaming";

interface HealthTrackerResult {
  providers: ProviderConfig[];
}

function classifyFailureForLog(status: number, message: string): string {
  const msg = message.toLowerCase();
  if (status === 402 || msg.includes("quota") || msg.includes("credit") || msg.includes("billing")) return "quota";
  if (status === 429 || msg.includes("rate limit") || msg.includes("too many requests")) return "rate_limit";
  if (status === 504 || msg.includes("timeout")) return "timeout";
  if (status >= 500 && status < 600) return "server_error";
  if (msg.includes("connection") || msg.includes("refused")) return "connection";
  return "unknown";
}

async function getHealthyProviders(
  env: Env,
  plan: string,
  providerList: ProviderConfig[]
): Promise<ProviderConfig[]> {
  const id = env.HEALTH_TRACKER.idFromName("global");
  const stub = env.HEALTH_TRACKER.get(id);

  const res = await stub.fetch("https://fake-host/health", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      action: "getHealthyProviders",
      plan,
      providerList,
    }),
  });

  if (!res.ok) {
    // If DO fails, return all providers (fail open)
    return providerList;
  }

  const data = (await res.json()) as HealthTrackerResult;
  return data.providers;
}

async function reportFailure(
  env: Env,
  plan: string,
  provider: string,
  status: number,
  message: string
): Promise<void> {
  const id = env.HEALTH_TRACKER.idFromName("global");
  const stub = env.HEALTH_TRACKER.get(id);

  try {
    await stub.fetch("https://fake-host/health", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "recordFailure",
        plan,
        provider,
        status,
        message,
      }),
    });
  } catch {
    // Best-effort; don't fail the request if DO is unreachable
  }
}

async function reportSuccess(
  env: Env,
  plan: string,
  provider: string
): Promise<void> {
  const id = env.HEALTH_TRACKER.idFromName("global");
  const stub = env.HEALTH_TRACKER.get(id);

  try {
    await stub.fetch("https://fake-host/health", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "recordSuccess",
        plan,
        provider,
      }),
    });
  } catch {
    // Best-effort
  }
}

export async function routeRequest(
  req: RouterRequest,
  env: Env
): Promise<Response> {
  const planConfig = await getPlan(env, req.plan);
  if (!planConfig || planConfig.providers.length === 0) {
    return new Response(
      JSON.stringify({ error: `Plan "${req.plan}" not found or empty` }),
      { status: 404, headers: { "Content-Type": "application/json" } }
    );
  }

  // 1. Get healthy providers
  const healthyProviders = await getHealthyProviders(env, req.plan, planConfig.providers);
  if (healthyProviders.length === 0) {
    return new Response(
      JSON.stringify({ error: "All providers in cooldown" }),
      { status: 503, headers: { "Content-Type": "application/json" } }
    );
  }

  const errors: Array<{ provider: string; status: number; message: string }> = [];

  // 2. Try each healthy provider
  for (const provider of healthyProviders) {
    const apiKey = await getProviderKey(provider.name, env);
    if (!apiKey) {
      console.log(`[ROUTER] MISSING_API_KEY: plan=${req.plan} provider=${provider.name}`);
      errors.push({ provider: provider.name, status: 0, message: "Missing API key" });
      continue;
    }

    // Translate request to provider's native format
    let translatedReq: TranslatedRequest;
    try {
      translatedReq = translateRequestToProvider(req.body, req.clientFormat, provider.format, provider.model);
    } catch (err) {
      errors.push({
        provider: provider.name,
        status: 0,
        message: `Translation error: ${err instanceof Error ? err.message : String(err)}`,
      });
      continue;
    }

    // Call provider
    let providerRes: Response;
    try {
      providerRes = await callProvider(
        provider,
        apiKey,
        translatedReq.body,
        req.isStreaming
      );
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      console.log(`[ROUTER] PROVIDER_EXCEPTION: plan=${req.plan} provider=${provider.name} error="${message}"`);
      await reportFailure(env, req.plan, provider.name, 0, message);
      errors.push({ provider: provider.name, status: 0, message });
      continue;
    }

    // Check for HTTP errors
    if (!providerRes.ok) {
      let message = `HTTP ${providerRes.status}`;
      try {
        const body = await providerRes.text();
        message = body.substring(0, 500);
      } catch {
        // ignore
      }
      const failureType = classifyFailureForLog(providerRes.status, message);
      console.log(`[ROUTER] PROVIDER_HTTP_ERROR: plan=${req.plan} provider=${provider.name} status=${providerRes.status} type=${failureType} message="${message.replace(/\n/g, ' ')}"`);
      await reportFailure(env, req.plan, provider.name, providerRes.status, message);
      errors.push({ provider: provider.name, status: providerRes.status, message });
      continue;
    }

    // Success! Report it and return translated response
    await reportSuccess(env, req.plan, provider.name);

    // For streaming with matching formats: passthrough
    if (req.isStreaming && req.clientFormat === provider.format) {
      return new Response(providerRes.body, {
        status: 200,
        headers: {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          Connection: "keep-alive",
        },
      });
    }

    // For streaming with mismatched formats: real-time SSE translation
    if (req.isStreaming && req.clientFormat !== provider.format) {
      const transform = createSseTransformStream(provider.format, req.clientFormat);
      const stream = providerRes.body?.pipeThrough(transform);
      if (!stream) {
        return new Response("Provider returned empty stream", { status: 502 });
      }
      return new Response(stream, {
        status: 200,
        headers: {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          Connection: "keep-alive",
        },
      });
    }

    // Non-streaming: translate and return
    const responseBody = await providerRes.text();
    let parsedBody: unknown;
    try {
      parsedBody = JSON.parse(responseBody);
    } catch {
      parsedBody = { content: responseBody };
    }

    const clientResponse = translateResponseToClient(
      parsedBody,
      provider.format,
      req.clientFormat
    );

    return new Response(JSON.stringify(clientResponse), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  // All providers failed
  return new Response(
    JSON.stringify({
      error: "All providers failed",
      details: errors,
    }),
    { status: 503, headers: { "Content-Type": "application/json" } }
  );
}

function createSseTransformStream(
  providerFormat: ClientFormat,
  clientFormat: ClientFormat
): TransformStream<Uint8Array, Uint8Array> {
  let buffer = "";

  const transform = providerFormat === "anthropic" && clientFormat === "openai"
    ? createAnthropicSseToOpenAiTranslator()
    : providerFormat === "openai" && clientFormat === "anthropic"
    ? createOpenAiSseToAnthropicTranslator()
    : null;

  return new TransformStream({
    transform(chunk, controller) {
      buffer += new TextDecoder().decode(chunk, { stream: true });

      // Process complete lines (events may span chunks)
      let lastIndex = 0;
      for (let i = 0; i < buffer.length; i++) {
        if (buffer[i] === "\n" && buffer[i + 1] === "\n") {
          const eventText = buffer.slice(lastIndex, i + 2);
          lastIndex = i + 2;

          if (transform) {
            const events = parseSseEvents(eventText);
            const translated = transform(events);
            const output = serializeSseEvents(translated);
            controller.enqueue(new TextEncoder().encode(output));
          } else {
            controller.enqueue(new TextEncoder().encode(eventText));
          }
        }
      }

      // Keep incomplete data in buffer
      buffer = buffer.slice(lastIndex);
    },

    flush(controller) {
      if (buffer.length > 0) {
        if (transform) {
          const events = parseSseEvents(buffer);
          const translated = transform(events);
          const output = serializeSseEvents(translated);
          controller.enqueue(new TextEncoder().encode(output));
        } else {
          controller.enqueue(new TextEncoder().encode(buffer));
        }
        buffer = "";
      }
    },
  });
}
