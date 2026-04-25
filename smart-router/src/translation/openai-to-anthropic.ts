/**
 * OpenAI Chat Completions Request → Anthropic Messages Request
 */

import { openaiToolsToAnthropic, openaiToolChoiceToAnthropic } from "./tools";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function translateOpenAiRequestToAnthropic(body: any, overrideModel?: string): any {
  const result: Record<string, unknown> = {};

  // Model
  result.model = overrideModel || body.model || "claude-sonnet-4-6";

  // Extract system messages
  const systemParts: Array<{ type: "text"; text: string }> = [];
  const messages: any[] = [];

  for (const msg of body.messages || []) {
    if (msg.role === "system") {
      const text = typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content);
      systemParts.push({ type: "text", text });
    } else {
      messages.push(msg);
    }
  }

  if (systemParts.length === 1) {
    result.system = systemParts[0].text;
  } else if (systemParts.length > 1) {
    result.system = systemParts;
  }

  // Convert messages
  result.messages = messages.map((msg) => {
    if (msg.role === "tool") {
      // tool result → user message with tool_result content
      return {
        role: "user",
        content: [{
          type: "tool_result",
          tool_use_id: msg.tool_call_id,
          content: typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content),
        }],
      };
    }

    if (msg.role === "assistant" && msg.tool_calls) {
      // assistant with tool_calls → assistant with tool_use blocks
      const content: any[] = [];
      if (msg.content) {
        content.push({ type: "text", text: msg.content });
      }
      for (const tc of msg.tool_calls) {
        content.push({
          type: "tool_use",
          id: tc.id,
          name: tc.function?.name || tc.name,
          input: (() => {
            try {
              return JSON.parse(tc.function?.arguments || tc.arguments || "{}");
            } catch {
              return {};
            }
          })(),
        });
      }
      return { role: "assistant", content };
    }

    // Standard user/assistant message
    if (Array.isArray(msg.content)) {
      // Already content blocks (images, etc.)
      return {
        role: msg.role,
        content: msg.content.map((c: any) => {
          if (c.type === "image_url") {
            return {
              type: "image",
              source: {
                type: "base64",
                media_type: c.image_url?.url?.match(/^data:([^;]+);/)?.[1] || "image/png",
                data: c.image_url?.url?.replace(/^data:[^;]+;base64,/, "") || "",
              },
            };
          }
          return c;
        }),
      };
    }

    return msg;
  });

  // Tools
  if (body.tools?.length) {
    result.tools = openaiToolsToAnthropic(body.tools);
  }

  // Tool choice
  if (body.tool_choice !== undefined) {
    result.tool_choice = openaiToolChoiceToAnthropic(body.tool_choice);
  }

  // Other params
  result.max_tokens = body.max_tokens ?? 4096;
  if (body.temperature !== undefined) result.temperature = body.temperature;
  if (body.top_p !== undefined) result.top_p = body.top_p;
  if (body.stream !== undefined) result.stream = body.stream;

  return result;
}
