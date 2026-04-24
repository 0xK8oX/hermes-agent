/**
 * Anthropic Messages Request → OpenAI Chat Completions Request
 */

import { anthropicToolsToOpenai, anthropicToolChoiceToOpenai } from "./tools";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function translateAnthropicRequestToOpenAi(body: any, overrideModel?: string): any {
  const result: Record<string, unknown> = {};

  // Model
  result.model = overrideModel || body.model || "auto";

  // Build messages array
  const messages: any[] = [];

  // Prepend system as first message
  if (body.system !== undefined) {
    const systemText = Array.isArray(body.system)
      ? body.system.map((s: any) => s.text || "").join("\n")
      : String(body.system);
    messages.push({ role: "system", content: systemText });
  }

  // Convert messages
  for (const msg of body.messages || []) {
    if (msg.role === "assistant" && Array.isArray(msg.content)) {
      // Check for tool_use blocks
      const textParts: string[] = [];
      const toolCalls: any[] = [];

      for (const block of msg.content) {
        if (block.type === "text") {
          textParts.push(block.text);
        } else if (block.type === "tool_use") {
          toolCalls.push({
            id: block.id,
            type: "function",
            function: {
              name: block.name,
              arguments: JSON.stringify(block.input || {}),
            },
          });
        }
      }

      const assistantMsg: any = { role: "assistant" };
      if (textParts.length > 0) {
        assistantMsg.content = textParts.join("");
      }
      if (toolCalls.length > 0) {
        assistantMsg.tool_calls = toolCalls;
        if (!assistantMsg.content) {
          assistantMsg.content = null;
        }
      }
      messages.push(assistantMsg);
    } else if (msg.role === "user" && Array.isArray(msg.content)) {
      // Check for tool_result blocks
      const toolResults = msg.content.filter((c: any) => c.type === "tool_result");
      const otherParts = msg.content.filter((c: any) => c.type !== "tool_result");

      // Emit tool_result messages
      for (const tr of toolResults) {
        messages.push({
          role: "tool",
          tool_call_id: tr.tool_use_id,
          content: typeof tr.content === "string" ? tr.content : JSON.stringify(tr.content),
        });
      }

      // Emit remaining user content
      if (otherParts.length > 0) {
        messages.push({
          role: "user",
          content: otherParts.map((c: any) => {
            if (c.type === "image") {
              return {
                type: "image_url",
                image_url: {
                  url: `data:${c.source?.media_type || "image/png"};base64,${c.source?.data || ""}`,
                },
              };
            }
            return c;
          }),
        });
      }
    } else {
      messages.push(msg);
    }
  }

  result.messages = messages;

  // Tools
  if (body.tools?.length) {
    result.tools = anthropicToolsToOpenai(body.tools);
  }

  // Tool choice
  if (body.tool_choice !== undefined) {
    result.tool_choice = anthropicToolChoiceToOpenai(body.tool_choice);
  }

  // Other params
  if (body.max_tokens !== undefined) result.max_tokens = body.max_tokens;
  if (body.temperature !== undefined) result.temperature = body.temperature;
  if (body.top_p !== undefined) result.top_p = body.top_p;
  if (body.stream !== undefined) result.stream = body.stream;

  return result;
}
