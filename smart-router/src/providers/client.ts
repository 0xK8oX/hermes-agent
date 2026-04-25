/**
 * Smart Router - Provider HTTP Client
 *
 * Makes requests to upstream LLM providers with timeout handling.
 * Detects auth style: native Anthropic (x-api-key) vs OpenAI-compatible (Bearer).
 */

import type { ProviderConfig } from "../types";

function isNativeAnthropic(baseUrl: string): boolean {
  return baseUrl.includes("anthropic.com") || baseUrl.includes("claude-api");
}

function buildEndpoint(baseUrl: string, format: string): string {
  const base = baseUrl.replace(/\/v\d+$/, "");
  if (format === "anthropic") {
    return `${base}/v1/messages`;
  }
  return `${base}/v1/chat/completions`;
}

export async function callProvider(
  provider: ProviderConfig,
  apiKey: string,
  body: string,
  isStreaming: boolean
): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), provider.timeout * 1000);

  try {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };

    if (isNativeAnthropic(provider.base_url)) {
      headers["x-api-key"] = apiKey;
      headers["anthropic-version"] = "2023-06-01";
    } else {
      headers["Authorization"] = `Bearer ${apiKey}`;
    }

    const endpoint = buildEndpoint(provider.base_url, provider.format);

    const response = await fetch(endpoint, {
      method: "POST",
      headers,
      body,
      signal: controller.signal,
    });

    clearTimeout(timeoutId);
    return response;
  } catch (err) {
    clearTimeout(timeoutId);
    if (err instanceof Error && err.name === "AbortError") {
      console.log(`[PROVIDER] TIMEOUT: ${provider.name} (${provider.base_url}) after ${provider.timeout}s`);
      return new Response(JSON.stringify({ error: "timeout" }), {
        status: 504,
        headers: { "Content-Type": "application/json" },
      });
    }
    console.log(`[PROVIDER] CONNECTION_ERROR: ${provider.name} (${provider.base_url}) - ${err instanceof Error ? err.message : String(err)}`);
    throw err;
  }
}
