/**
 * OpenAI Chat Completions Response → Anthropic Messages Response
 */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function translateOpenAiResponseToAnthropic(body: any): any {
  const choice = body.choices?.[0];
  const message = choice?.message || {};

  const content: any[] = [];

  // Text content
  if (message.content) {
    content.push({ type: "text", text: message.content });
  }

  // Tool calls → tool_use blocks
  if (message.tool_calls?.length) {
    for (const tc of message.tool_calls) {
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
  }

  // Reasoning/thinking
  if (message.reasoning) {
    content.push({
      type: "thinking",
      thinking: message.reasoning,
    });
  }

  // If no content at all, add empty text
  if (content.length === 0) {
    content.push({ type: "text", text: "" });
  }

  const stopReason = choice?.finish_reason;
  let anthropicStopReason = "end_turn";
  if (stopReason === "tool_calls" || stopReason === "function_call") {
    anthropicStopReason = "tool_use";
  } else if (stopReason === "length") {
    anthropicStopReason = "max_tokens";
  }

  return {
    id: body.id || `msg_${Date.now()}`,
    type: "message",
    role: "assistant",
    model: body.model || "unknown",
    content,
    stop_reason: anthropicStopReason,
    usage: {
      input_tokens: body.usage?.prompt_tokens || 0,
      output_tokens: body.usage?.completion_tokens || 0,
    },
  };
}
