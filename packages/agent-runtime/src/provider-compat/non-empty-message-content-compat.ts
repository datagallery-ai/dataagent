import type { ProcessLLMRequestArgs, ProcessLLMRequestResult, Processor } from "@mastra/core/processors";

const EMPTY_TOOL_RESULT = "(empty tool result)";
const PLACEHOLDER_TEXT = ".";

type PromptPart = Record<string, unknown>;
type PromptMessage = {
  role: string;
  content: unknown;
  providerOptions?: unknown;
};

type PromptCompatModelProvider = {
  prompt_compat?: {
    requires_non_empty_message_content?: boolean;
  };
};

export function shouldApplyNonEmptyMessageContentCompat(modelProvider: PromptCompatModelProvider): boolean {
  return modelProvider.prompt_compat?.requires_non_empty_message_content === true;
}

/** Some OpenAI-compatible endpoints reject requests when a message lacks usable `content`. */
export function ensureNonEmptyMessageContentPrompt<T extends PromptMessage>(prompt: T[]): T[] {
  return prompt.map((message) => {
    if (message.role === "system") {
      const content =
        typeof message.content === "string" && message.content.trim().length > 0
          ? message.content
          : PLACEHOLDER_TEXT;
      return { ...message, content };
    }

    if (message.role === "user") {
      const content = normalizeUserContent(message.content);
      return { ...message, content };
    }

    if (message.role === "assistant") {
      const content = normalizeAssistantContent(message.content);
      return { ...message, content };
    }

    if (message.role === "tool") {
      const content = normalizeToolContent(message.content);
      return { ...message, content };
    }

    return message;
  });
}

export class NonEmptyMessageContentCompatProcessor implements Processor<"non-empty-message-content-compat"> {
  readonly id = "non-empty-message-content-compat";
  readonly name = "Non-Empty Message Content Compat";

  constructor(private readonly enabled: boolean) {}

  processLLMRequest(args: ProcessLLMRequestArgs): ProcessLLMRequestResult {
    if (!this.enabled) return undefined;
    return { prompt: ensureNonEmptyMessageContentPrompt(args.prompt) };
  }
}

function normalizeUserContent(content: unknown): PromptPart[] {
  if (!Array.isArray(content) || content.length === 0) {
    return [{ type: "text", text: PLACEHOLDER_TEXT }];
  }

  const normalized = content.map((part) => {
    if (!isRecord(part)) return part;
    if (part.type === "text" && typeof part.text === "string" && part.text.trim().length === 0) {
      return { ...part, text: PLACEHOLDER_TEXT };
    }
    return part;
  });

  const hasRenderable = normalized.some((part) => {
    if (!isRecord(part)) return true;
    if (part.type !== "text") return true;
    return typeof part.text === "string" && part.text.trim().length > 0;
  });

  return hasRenderable ? normalized : [{ type: "text", text: PLACEHOLDER_TEXT }];
}

function normalizeAssistantContent(content: unknown): PromptPart[] {
  if (!Array.isArray(content) || content.length === 0) {
    return [{ type: "text", text: PLACEHOLDER_TEXT }];
  }

  let normalized = content.map((part) => {
    if (!isRecord(part)) return part;
    if (part.type === "text" && typeof part.text === "string" && part.text.trim().length === 0) {
      return { ...part, text: PLACEHOLDER_TEXT };
    }
    if (part.type === "tool-result") {
      return { ...part, output: normalizeToolResultOutput(part.output) };
    }
    return part;
  });

  const hasText = normalized.some(
    (part) => isRecord(part) && part.type === "text" && typeof part.text === "string" && part.text.trim().length > 0,
  );

  // Some endpoints reject assistant messages whose OpenAI conversion yields null/empty content.
  if (!hasText) {
    normalized = [{ type: "text", text: PLACEHOLDER_TEXT }, ...normalized];
  }

  return normalized.length > 0 ? normalized : [{ type: "text", text: PLACEHOLDER_TEXT }];
}

function normalizeToolContent(content: unknown): PromptPart[] {
  if (!Array.isArray(content) || content.length === 0) {
    return [
      {
        type: "tool-result",
        toolCallId: "unknown",
        toolName: "unknown",
        output: { type: "text", value: EMPTY_TOOL_RESULT },
      },
    ];
  }

  return content.map((part) => {
    if (!isRecord(part) || part.type !== "tool-result") return part;
    return { ...part, output: normalizeToolResultOutput(part.output) };
  });
}

function normalizeToolResultOutput(output: unknown): Record<string, unknown> {
  if (!isRecord(output)) {
    return { type: "text", value: EMPTY_TOOL_RESULT };
  }

  if (output.type === "text") {
    const value = typeof output.value === "string" ? output.value : "";
    return { type: "text", value: value.trim().length > 0 ? value : EMPTY_TOOL_RESULT };
  }

  if (output.type === "error-text") {
    const value = typeof output.value === "string" ? output.value : "";
    return { type: "error-text", value: value.trim().length > 0 ? value : EMPTY_TOOL_RESULT };
  }

  if (output.type === "json") {
    if (output.value === null || output.value === undefined) {
      return { type: "text", value: EMPTY_TOOL_RESULT };
    }
    return output;
  }

  if (output.type === "content" && Array.isArray(output.value)) {
    const value = output.value.map((part) => {
      if (!isRecord(part)) return part;
      if (part.type === "text" && typeof part.text === "string" && part.text.trim().length === 0) {
        return { ...part, text: PLACEHOLDER_TEXT };
      }
      return part;
    });
    const hasRenderable = value.some(
      (part) => isRecord(part) && part.type === "text" && typeof part.text === "string" && part.text.trim().length > 0,
    );
    if (!hasRenderable) {
      return { type: "text", value: EMPTY_TOOL_RESULT };
    }
    return { ...output, value };
  }

  if (output.type === "error-json") {
    if (output.value === null || output.value === undefined) {
      return { type: "text", value: EMPTY_TOOL_RESULT };
    }
    return output;
  }

  return output;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;
