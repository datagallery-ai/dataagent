export type ContextBudget = {
  maxTokens?: number;
  maxRows?: number;
  maxChars?: number;
  sourceLimits?: Record<string, number>;
};
