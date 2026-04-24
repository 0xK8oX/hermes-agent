/**
 * Anthropic Messages Response → OpenAI Chat Completions Response
 */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function translateAnthropicResponseToOpenAi(body: any): any {
  const contentBlocks = body.content || [];
  const textParts: string[] = [];
  const toolCalls: any[] = [];
  let reasoning: string | undefined;

  for (const block of contentBlocks) {
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
    } else if (block.type === "thinking") {
      reasoning = (reasoning || "") + block.thinking;
    }
  }

  const message: any = {
    role: body.role || "assistant",
    content: textParts.length > 0 ? textParts.join("") : null,
  };

  if (toolCalls.length > 0) {
    message.tool_calls = toolCalls;
  }

  if (reasoning) {
    message.reasoning = reasoning;
  }

  return {
    id: body.id || `chatcmpl-${Date.now()}`,
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model: body.model || "unknown",
    choices: [
      {
        index: 0,
        message,
        finish_reason: body.stop_reason === "tool_use" ? "tool_calls" : body.stop_reason || "stop",
      },
    ],
    usage: {
      prompt_tokens: body.usage?.input_tokens || 0,
      completion_tokens: body.usage?.output_tokens || 0,
      total_tokens: (body.usage?.input_tokens || 0) + (body.usage?.output_tokens || 0),
    },
  };
}
