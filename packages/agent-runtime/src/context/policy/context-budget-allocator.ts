import { CONTEXT_MAX_CHARS, CONTEXT_MAX_TOKENS } from "../inventory/context-limits.js";
import { ContextTokenCounter } from "./context-token-counter.js";
import type { ContextBudget } from "../inventory/context-budget.js";

export type { ContextBudget } from "../inventory/context-budget.js";

export type ContextSourceLimitProfiles = Record<string, Record<string, number>>;

export type ContextBudgetAllocatorOptions = {
  sourceLimitProfiles?: ContextSourceLimitProfiles;
  tokenCounter?: ContextTokenCounter;
};

export type AllocateRequest = {
  sourceType: string;
  toolName?: string;
};

export class ContextBudgetAllocator {
  private readonly sourceLimitProfiles: ContextSourceLimitProfiles;
  private readonly tokenCounter: ContextTokenCounter;

  constructor(options: ContextBudgetAllocatorOptions = {}) {
    this.sourceLimitProfiles = { ...(options.sourceLimitProfiles ?? {}) };
    this.tokenCounter = options.tokenCounter ?? new ContextTokenCounter();
  }

  allocate(request: AllocateRequest): ContextBudget {
    return {
      maxTokens: CONTEXT_MAX_TOKENS,
      maxChars: CONTEXT_MAX_CHARS,
      sourceLimits: this.resolveSourceLimits(request)
    };
  }

  async countTokens(text: string, modelName?: string): Promise<number> {
    return this.tokenCounter.countTokens(text, modelName);
  }

  // Sync version - uses cached tokenizer or falls back to estimation
  countTokensSync(text: string, modelName?: string): number {
    return this.tokenCounter.countTokensSync(text, modelName);
  }

  private resolveSourceLimits(request: AllocateRequest): Record<string, number> {
    const prefixProfiles = request.toolName
      ? Object.entries(this.sourceLimitProfiles)
          .filter(([key]) => key.endsWith("*") && request.toolName?.startsWith(key.slice(0, -1)))
          .map(([, value]) => value)
      : [];
    return {
      ...(this.sourceLimitProfiles[request.sourceType] ?? {}),
      ...Object.assign({}, ...prefixProfiles),
      ...(request.toolName ? this.sourceLimitProfiles[request.toolName] ?? {} : {})
    };
  }
}
