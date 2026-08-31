import type { PromptTokenReport } from "../inventory/context-token-report.js";
import type { ContextPromptMessage } from "./context-prompt-message.js";

export type ContextPromptView = {
  systemMessages: unknown[];
  messages: ContextPromptMessage[];
  tokenReport: PromptTokenReport;
};
