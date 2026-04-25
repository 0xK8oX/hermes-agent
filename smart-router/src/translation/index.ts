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

  // Only override model when client sends "auto" or nothing.
  // If client sends a specific model, use that instead.
  const modelToUse = (body.model === "auto" || !body.model) ? overrideModel : body.model;

  if (clientFormat === providerFormat) {
    translated = modelToUse ? { ...body, model: modelToUse } : body;
  } else if (clientFormat === "openai" && providerFormat === "anthropic") {
    translated = translateOpenAiRequestToAnthropic(body, modelToUse);
  } else if (clientFormat === "anthropic" && providerFormat === "openai") {
    translated = translateAnthropicRequestToOpenAi(body, modelToUse);
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
