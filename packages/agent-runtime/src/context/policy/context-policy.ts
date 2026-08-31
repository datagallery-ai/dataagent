import type { ContextBudget } from "../inventory/context-budget.js";
import type { ContextItem } from "../inventory/context-item.js";

// Source adapters shape structured values; this policy is the final fail-closed budget boundary.
export class ContextPolicy {
  applyBudget(
    items: ContextItem[],
    budget: ContextBudget,
    countTokens: (text: string) => number
  ): ContextItem[] {
    for (const visibility of ["model", "activity"] as const) {
      const serialized = JSON.stringify(
        items.filter((item) => item.visibility === visibility).map((item) => item.content)
      );
      if (budget.maxChars !== undefined && serialized.length > budget.maxChars) {
        throw new Error(`CONTEXT_CHAR_BUDGET_EXCEEDED:${visibility}`);
      }
      if (budget.maxTokens !== undefined && countTokens(serialized) > budget.maxTokens) {
        throw new Error(`CONTEXT_TOKEN_BUDGET_EXCEEDED:${visibility}`);
      }
    }
    return items;
  }

  truncate(items: ContextItem[], maxItems: number): ContextItem[] {
    return items.slice(0, maxItems);
  }

  redact(items: ContextItem[]): ContextItem[] {
    return items;
  }
}
