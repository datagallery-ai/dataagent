export type PromptTokenReport = {
  systemTokens: number;
  toolTokens: number;
  messageTokens: number;
  totalInputTokens: number;
  inputBudget: number;
  remainingTokens: number;
  countQuality: "cached-tokenizer" | "estimated";
};
