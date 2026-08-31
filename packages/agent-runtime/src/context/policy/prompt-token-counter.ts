import type { ModelContextProfile } from "./model-context-profile.js";
import { ContextTokenCounter } from "./context-token-counter.js";
import type { PromptTokenReport } from "../inventory/context-token-report.js";

export class PromptTokenCounter {
  constructor(private readonly tokenCounter: ContextTokenCounter = new ContextTokenCounter()) {}

  count(input: {
    systemMessages: unknown[];
    tools?: Record<string, unknown>;
    messages: unknown[];
    modelName: string | undefined;
    profile: ModelContextProfile;
  }): PromptTokenReport {
    const messageTokens = this.countMessages(input.messages, input.modelName, input.profile);
    return this.countPrecomputedMessages({ ...input, messageTokens });
  }

  countPrecomputedMessages(input: {
    systemMessages: unknown[];
    tools?: Record<string, unknown>;
    messageTokens: number;
    modelName: string | undefined;
    profile: ModelContextProfile;
  }): PromptTokenReport {
    const systemTokens = this.countValue(input.systemMessages, input.modelName)
      + input.systemMessages.length * input.profile.messageOverhead;
    const toolEntries = Object.entries(input.tools ?? {});
    const toolTokens = this.countValue(toolEntries, input.modelName)
      + toolEntries.length * input.profile.toolSchemaOverhead;
    const totalInputTokens = systemTokens + toolTokens + input.messageTokens;
    const inputBudget = Math.max(
      input.profile.contextWindow - input.profile.outputReserve - input.profile.safetyMargin,
      0
    );

    return {
      systemTokens,
      toolTokens,
      messageTokens: input.messageTokens,
      totalInputTokens,
      inputBudget,
      remainingTokens: inputBudget - totalInputTokens,
      countQuality: "estimated"
    };
  }

  countMessages(messages: unknown[], modelName: string | undefined, profile: ModelContextProfile): number {
    return this.countValue(messages, modelName) + messages.length * profile.messageOverhead;
  }

  countProviderPrompt(prompt: unknown, modelName: string | undefined): number {
    return this.countValue(prompt, modelName);
  }

  private countValue(value: unknown, modelName?: string): number {
    return this.tokenCounter.countTokensSync(safeSerialize(value), modelName);
  }
}

const safeSerialize = (value: unknown): string => {
  const seen = new WeakSet<object>();

  return JSON.stringify(value, (_key, entry: unknown) => {
    if (typeof entry === "function") {
      return `[function:${entry.name || "anonymous"}]`;
    }
    if (typeof entry !== "object" || entry === null) {
      return entry;
    }
    if (seen.has(entry)) {
      return "[circular]";
    }
    seen.add(entry);
    return entry;
  });
};
