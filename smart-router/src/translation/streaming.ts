/**
 * Smart Router - SSE Stream Transformers
 *
 * For same-format routes: passthrough (handled in router.ts)
 * For cross-format routes with streaming: these transformers handle
 * real-time SSE event translation.
 */

/**
 * Parse SSE data lines from a chunk of text.
 * Returns array of parsed JSON objects (or strings for [DONE]).
 */
export function parseSseEvents(chunk: string): Array<{ event?: string; data: unknown }> {
  const events: Array<{ event?: string; data: unknown }> = [];
  const lines = chunk.split("\n");
  let currentEvent: string | undefined;

  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith("event:")) {
      currentEvent = trimmed.slice(6).trim();
    } else if (trimmed.startsWith("data:")) {
      const dataStr = trimmed.slice(5).trim();
      if (dataStr === "[DONE]") {
        events.push({ event: currentEvent, data: "[DONE]" });
      } else {
        try {
          events.push({ event: currentEvent, data: JSON.parse(dataStr) });
        } catch {
          events.push({ event: currentEvent, data: dataStr });
        }
      }
      currentEvent = undefined;
    }
  }

  return events;
}

/**
 * Serialize events back to SSE text format.
 */
export function serializeSseEvents(events: Array<{ event?: string; data: unknown }>): string {
  const lines: string[] = [];
  for (const ev of events) {
    if (ev.event) {
      lines.push(`event: ${ev.event}`);
    }
    if (ev.data === "[DONE]") {
      lines.push("data: [DONE]");
    } else {
      lines.push(`data: ${JSON.stringify(ev.data)}`);
    }
    lines.push("");
  }
  return lines.join("\n");
}

export type SseTranslator = (events: Array<{ event?: string; data: unknown }>) => Array<{ event?: string; data: unknown }>;

/**
 * Create a stateful translator that transforms Anthropic SSE events to OpenAI SSE format.
 * The returned function maintains state (e.g. tool_call buffers) across calls.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function createAnthropicSseToOpenAiTranslator(): SseTranslator {
  let currentToolUse: { id: string; name: string; input: string } | null = null;

  return function (events: Array<{ event?: string; data: unknown }>): Array<{ event?: string; data: unknown }> {
    const result: Array<{ event?: string; data: unknown }> = [];

    for (const ev of events) {
      const data = ev.data as any;

      if (ev.event === "message_start") {
        result.push({
          data: {
            id: data.message?.id || `chatcmpl-${Date.now()}`,
            object: "chat.completion.chunk",
            created: Math.floor(Date.now() / 1000),
            model: data.message?.model || "unknown",
            choices: [{ index: 0, delta: { role: "assistant" } }],
          },
        });
      } else if (ev.event === "content_block_start") {
        if (data.content_block?.type === "tool_use") {
          currentToolUse = {
            id: data.content_block.id,
            name: data.content_block.name,
            input: "",
          };
        }
      } else if (ev.event === "content_block_delta") {
        const delta = data.delta;
        if (delta?.type === "text_delta") {
          result.push({
            data: {
              object: "chat.completion.chunk",
              choices: [{ index: 0, delta: { content: delta.text } }],
            },
          });
        } else if (delta?.type === "input_json_delta") {
          if (currentToolUse) {
            currentToolUse.input += delta.partial_json || "";
          }
        } else if (delta?.type === "thinking_delta") {
          result.push({
            data: {
              object: "chat.completion.chunk",
              choices: [{ index: 0, delta: { reasoning: delta.thinking } }],
            },
          });
        }
      } else if (ev.event === "content_block_stop") {
        if (currentToolUse) {
          result.push({
            data: {
              object: "chat.completion.chunk",
              choices: [{
                index: 0,
                delta: {
                  tool_calls: [{
                    index: 0,
                    id: currentToolUse.id,
                    type: "function",
                    function: {
                      name: currentToolUse.name,
                      arguments: currentToolUse.input,
                    },
                  }],
                },
              }],
            },
          });
          currentToolUse = null;
        }
      } else if (ev.event === "message_delta") {
        const usage = data.usage;
        if (usage) {
          result.push({
            data: {
              object: "chat.completion.chunk",
              choices: [],
              usage: {
                prompt_tokens: usage.input_tokens || 0,
                completion_tokens: usage.output_tokens || 0,
              },
            },
          });
        }
        if (data.stop_reason) {
          const finishReason = data.stop_reason === "tool_use" ? "tool_calls" : data.stop_reason;
          result.push({
            data: {
              object: "chat.completion.chunk",
              choices: [{ index: 0, delta: {}, finish_reason: finishReason }],
            },
          });
        }
      } else if (ev.event === "message_stop" || data === "[DONE]") {
        result.push({ data: "[DONE]" });
      }
    }

    return result;
  };
}

/**
 * Create a stateful translator that transforms OpenAI SSE events to Anthropic SSE format.
 * The returned function maintains state (e.g. tool_call buffers) across calls.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function createOpenAiSseToAnthropicTranslator(): SseTranslator {
  let toolCallBuffer: { id: string; name: string; arguments: string } | null = null;
  let blockIndex = 0;

  return function (events: Array<{ event?: string; data: unknown }>): Array<{ event?: string; data: unknown }> {
    const result: Array<{ event?: string; data: unknown }> = [];

    for (const ev of events) {
      const data = ev.data as any;
      if (data === "[DONE]") {
        result.push({ event: "message_stop", data: { type: "message_stop" } });
        continue;
      }

      const delta = data.choices?.[0]?.delta;
      if (!delta) continue;

      // Text content
      if (delta.content) {
        result.push({
          event: "content_block_delta",
          data: {
            type: "content_block_delta",
            index: blockIndex++,
            delta: { type: "text_delta", text: delta.content },
          },
        });
      }

      // Reasoning
      if (delta.reasoning) {
        result.push({
          event: "content_block_delta",
          data: {
            type: "content_block_delta",
            index: blockIndex++,
            delta: { type: "thinking_delta", thinking: delta.reasoning },
          },
        });
      }

      // Tool calls
      if (delta.tool_calls?.length) {
        const tc = delta.tool_calls[0];
        if (tc.id && tc.function?.name) {
          // New tool call starting
          if (toolCallBuffer) {
            // Finish previous
            result.push({
              event: "content_block_stop",
              data: { type: "content_block_stop", index: blockIndex },
            });
          }
          toolCallBuffer = {
            id: tc.id,
            name: tc.function.name,
            arguments: tc.function.arguments || "",
          };
          result.push({
            event: "content_block_start",
            data: {
              type: "content_block_start",
              index: blockIndex,
              content_block: {
                type: "tool_use",
                id: tc.id,
                name: tc.function.name,
                input: {},
              },
            },
          });
        } else if (tc.function?.arguments && toolCallBuffer) {
          // Continuation of current tool call
          toolCallBuffer.arguments += tc.function.arguments;
          result.push({
            event: "content_block_delta",
            data: {
              type: "content_block_delta",
              index: blockIndex,
              delta: { type: "input_json_delta", partial_json: tc.function.arguments },
            },
          });
        }
      }

      // Finish reason
      if (data.choices?.[0]?.finish_reason) {
        if (toolCallBuffer) {
          result.push({
            event: "content_block_stop",
            data: { type: "content_block_stop", index: blockIndex },
          });
          toolCallBuffer = null;
        }
        const stopReason = data.choices[0].finish_reason === "tool_calls" ? "tool_use" : data.choices[0].finish_reason;
        result.push({
          event: "message_delta",
          data: {
            type: "message_delta",
            delta: { stop_reason: stopReason },
          },
        });
      }
    }

    return result;
  };
}

// Keep pure-function versions for backward compatibility / tests
/** @deprecated Use createAnthropicSseToOpenAiTranslator for streaming */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function anthropicSseToOpenAi(events: Array<{ event?: string; data: unknown }>): Array<{ event?: string; data: unknown }> {
  return createAnthropicSseToOpenAiTranslator()(events);
}

/** @deprecated Use createOpenAiSseToAnthropicTranslator for streaming */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function openAiSseToAnthropic(events: Array<{ event?: string; data: unknown }>): Array<{ event?: string; data: unknown }> {
  return createOpenAiSseToAnthropicTranslator()(events);
}
