/**
 * Smart Router - Tool schema translation
 *
 * OpenAI format: { type: "function", function: { name, description, parameters } }
 * Anthropic format: { name, description, input_schema }
 */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function openaiToolsToAnthropic(tools: any[]): any[] {
  return tools.map((tool) => {
    const fn = tool.function || tool;
    return {
      name: fn.name,
      description: fn.description,
      input_schema: fn.parameters || fn.input_schema || { type: "object", properties: {} },
    };
  });
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function anthropicToolsToOpenai(tools: any[]): any[] {
  return tools.map((tool) => ({
    type: "function",
    function: {
      name: tool.name,
      description: tool.description,
      parameters: tool.input_schema || { type: "object", properties: {} },
    },
  }));
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function openaiToolChoiceToAnthropic(toolChoice: any): any {
  if (typeof toolChoice === "string") {
    if (toolChoice === "auto") return { type: "auto" };
    if (toolChoice === "required" || toolChoice === "any") return { type: "any" };
    if (toolChoice === "none") return { type: "none" };
    return { type: "auto" };
  }
  if (typeof toolChoice === "object" && toolChoice !== null) {
    if (toolChoice.type === "function" && toolChoice.function?.name) {
      return { type: "tool", name: toolChoice.function.name };
    }
    if (toolChoice.type === "tool" && toolChoice.name) {
      return toolChoice;
    }
  }
  return { type: "auto" };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function anthropicToolChoiceToOpenai(toolChoice: any): any {
  if (typeof toolChoice === "string") {
    if (toolChoice === "auto") return "auto";
    if (toolChoice === "any") return "required";
    if (toolChoice === "none") return "none";
    return "auto";
  }
  if (typeof toolChoice === "object" && toolChoice !== null) {
    if (toolChoice.type === "tool" && toolChoice.name) {
      return {
        type: "function",
        function: { name: toolChoice.name },
      };
    }
    if (toolChoice.type === "auto") return "auto";
    if (toolChoice.type === "any") return "required";
    if (toolChoice.type === "none") return "none";
  }
  return "auto";
}
