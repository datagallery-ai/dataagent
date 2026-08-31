import type { ProcessLLMRequestArgs, ProcessLLMRequestResult, Processor } from "@mastra/core/processors";

import { ModelContextProfileRegistry } from "../../policy/model-context-profile.js";
import { PromptTokenCounter } from "../../policy/prompt-token-counter.js";
import type { ContextProtocolEventSink } from "../context-protocol-event-sink.js";

export type MastraProviderPromptGuardProcessorOptions = {
  eventSink: ContextProtocolEventSink;
  modelName: string | undefined;
  profileRegistry?: ModelContextProfileRegistry;
  tokenCounter?: PromptTokenCounter;
};

export class MastraProviderPromptGuardProcessor implements Processor<"provider-prompt-guard"> {
  readonly id = "provider-prompt-guard";
  readonly name = "Provider Prompt Guard";
  private readonly profileRegistry: ModelContextProfileRegistry;
  private readonly tokenCounter: PromptTokenCounter;

  constructor(private readonly options: MastraProviderPromptGuardProcessorOptions) {
    this.profileRegistry = options.profileRegistry ?? new ModelContextProfileRegistry();
    this.tokenCounter = options.tokenCounter ?? new PromptTokenCounter();
  }

  processLLMRequest(args: ProcessLLMRequestArgs): ProcessLLMRequestResult {
    const profile = this.profileRegistry.resolve(this.options.modelName);
    const promptTokens = this.tokenCounter.countProviderPrompt(args.prompt, this.options.modelName);
    const inputBudget = Math.max(profile.contextWindow - profile.outputReserve - profile.safetyMargin, 0);
    const remainingTokens = inputBudget - promptTokens;
    const unavailableToolReferences = findUnavailableToolReferences(args.prompt);
    this.options.eventSink.emitContextEvent("context.prompt-verified", {
      step_number: args.stepNumber,
      model_profile_id: profile.id,
      prompt_tokens: promptTokens,
      input_budget: inputBudget,
      remaining_tokens: remainingTokens,
      // R-017: stable top-level budget fields. `model` is the resolved model name;
      // `budget_tokens` aliases `input_budget`; `total_tokens` mirrors prompt_tokens for
      // the verified-prompt snapshot (no completion tokens yet at this stage).
      ...(this.options.modelName ? { model: this.options.modelName } : {}),
      total_tokens: promptTokens,
      budget_tokens: inputBudget,
      ...(unavailableToolReferences.length > 0
        ? { unavailable_tool_references: unavailableToolReferences }
        : {})
    });

    if (unavailableToolReferences.length > 0) {
      args.abort("PROMPT_REFERENCES_UNAVAILABLE_TOOL", {
        metadata: { stepNumber: args.stepNumber, toolNames: unavailableToolReferences }
      });
    }

    if (promptTokens > inputBudget) {
      args.abort("CONTEXT_FINAL_PROMPT_EXCEEDS_BUDGET", {
        metadata: { inputBudget, promptTokens, stepNumber: args.stepNumber }
      });
    }

    return undefined;
  }
}

const UNAVAILABLE_SYSTEM_TOOL_NAMES = ["updateWorkingMemory", "setWorkingMemory", "update-working-memory"] as const;

const findUnavailableToolReferences = (prompt: unknown): string[] => {
  const systemText = collectSystemText(prompt).join("\n");
  return UNAVAILABLE_SYSTEM_TOOL_NAMES.filter((toolName) => systemText.includes(toolName));
};

const collectSystemText = (value: unknown): string[] => {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.flatMap((entry) => {
    if (!isRecord(entry) || entry.role !== "system") {
      return [];
    }
    return contentText(entry.content);
  });
};

const contentText = (content: unknown): string[] => {
  if (typeof content === "string") {
    return [content];
  }
  if (!Array.isArray(content)) {
    return [];
  }
  return content.flatMap((part) =>
    isRecord(part) && typeof part.text === "string" ? [part.text] : []
  );
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;
