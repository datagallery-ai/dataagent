import type { ContextBudget } from "../inventory/context-budget.js";
import type { ContextItem } from "../inventory/context-item.js";

export interface ToolObservationAdapter {
  readonly toolName: string;
  readonly resultType: string;
  readonly sourceType: string;
  toContextItems(raw: unknown, budget: ContextBudget): ContextItem[];
}
