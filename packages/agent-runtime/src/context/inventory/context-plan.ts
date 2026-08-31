import type { PromptTokenReport } from "./context-token-report.js";

export type ContextDecision = {
  strategyId: string;
  affectedGroupIds: string[];
  affectedItemIds?: string[];
  tokenSavings: number;
  reason: string;
};

export type GlobalContextBudget = {
  contextWindow: number;
  outputReserve: number;
  safetyMargin: number;
  inputBudget: number;
};

export type ContextPlan = {
  planId: string;
  stepNumber: number;
  packageRevision: number;
  selectedGroupIds: string[];
  omittedGroupIds: string[];
  selectedSourceItemIds: string[];
  omittedSourceItemIds: string[];
  decisions: ContextDecision[];
  budget: GlobalContextBudget;
  tokenReport: PromptTokenReport;
};
