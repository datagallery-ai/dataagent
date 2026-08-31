import { ContextSourcePolicy } from "./context-source-policy.js";

export const DEFAULT_CONTEXT_SOURCE_AUTHORITY_ORDER: Record<string, string[]> = {
  "compact-conversation-memory": ["metadata-summary", "mastra-working-memory", "observational-summary"],
  "long-term-memory": ["metadata-ltm"]
};

export const createDefaultContextSourcePolicy = (): ContextSourcePolicy =>
  new ContextSourcePolicy({
    authorityOrder: DEFAULT_CONTEXT_SOURCE_AUTHORITY_ORDER
  });
