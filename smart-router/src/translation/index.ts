/**
 * Smart Router - Translation dispatch
 */

import type { ClientFormat } from "../types";
import { translateOpenAiRequestToAnthropic } from "./openai-to-anthropic";
import { translateAnthropicRequestToOpenAi } from "./anthropic-to-openai";
import { translateAnthropicResponseToOpenAi } from "./anthropic-response";
import { translateOpenAiResponseToAnthropic } from "./openai-response";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function translateRequestToProvider(
  body: any,
  clientFormat: ClientFormat,
  providerFormat: ClientFormat,
  overrideModel?: string
): { url: string; headers: Record<string, string>; body: string; providerFormat: ClientFormat } {
  let translated: unknown;

  if (clientFormat === providerFormat) {
    translated = body;
  } else if (clientFormat === "openai" && providerFormat === "anthropic") {
    translated = translateOpenAiRequestToAnthropic(body, overrideModel);
  } else if (clientFormat === "anthropic" && providerFormat === "openai") {
    translated = translateAnthropicRequestToOpenAi(body, overrideModel);
  } else {
    translated = body;
  }

  return {
    url: "", // filled by router
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(translated),
    providerFormat,
  };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function translateResponseToClient(
  body: any,
  providerFormat: ClientFormat,
  clientFormat: ClientFormat
): unknown {
  if (clientFormat === providerFormat) {
    return body;
  }

  if (providerFormat === "anthropic" && clientFormat === "openai") {
    return translateAnthropicResponseToOpenAi(body);
  }

  if (providerFormat === "openai" && clientFormat === "anthropic") {
    return translateOpenAiResponseToAnthropic(body);
  }

  return body;
}
