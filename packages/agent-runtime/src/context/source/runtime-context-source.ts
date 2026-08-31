import type { ContextBudget } from "../inventory/context-budget.js";
import type { ContextItem } from "../inventory/context-item.js";

export type RuntimeContextRunScope = {
  runId: string;
  sessionId: string;
  userId: string;
};

export type RuntimeContextSourceInput = {
  budget: ContextBudget;
} & RuntimeContextRunScope;

export interface RuntimeContextSource {
  readonly sourceType: string;
  collect(input: RuntimeContextSourceInput): ContextItem[] | Promise<ContextItem[]>;
}
