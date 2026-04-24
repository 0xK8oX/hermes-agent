/**
 * Smart Router - Shared TypeScript types
 */

export interface ProviderConfig {
  name: string;
  base_url: string;
  model: string;
  format: "openai" | "anthropic";
  timeout: number;
}

export interface PlanConfig {
  providers: ProviderConfig[];
}

export interface PlansConfig {
  plans: Record<string, PlanConfig>;
}

export interface ProviderHealth {
  status: "healthy" | "degraded" | "unhealthy";
  consecutiveFailures: number;
  lastFailureAt: number;
  cooldownUntil: number;
  lastFailureReason: string;
}

export type ClientFormat = "openai" | "anthropic";

export interface RouterRequest {
  body: unknown;
  clientFormat: ClientFormat;
  plan: string;
  isStreaming: boolean;
}

export interface TranslatedRequest {
  url: string;
  headers: Record<string, string>;
  body: string;
  providerFormat: ClientFormat;
}

export interface ProviderResponse {
  response: Response;
  providerFormat: ClientFormat;
}
