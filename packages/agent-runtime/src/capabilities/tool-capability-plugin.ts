import { z } from "zod";

import type { CapabilityPlugin } from "./types.js";

type ExecutableTool = {
  execute?: (...args: unknown[]) => unknown | Promise<unknown>;
};

export type CreateToolCapabilityPluginInput = {
  id: string;
  version?: string;
  tools: Record<string, ExecutableTool>;
  reduceAction?(domainState: unknown, actionName: string, result: unknown): unknown;
};

/** Adapt an existing tool record into a statically registered capability plugin. */
export const createToolCapabilityPlugin = (input: CreateToolCapabilityPluginInput): CapabilityPlugin => ({
  manifest: {
    id: input.id,
    version: input.version ?? "1",
    provides: Object.keys(input.tools)
  },
  actions: Object.entries(input.tools).map(([name, tool]) => {
    if (!tool.execute) {
      throw new Error(`TOOL_EXECUTE_REQUIRED:${name}`);
    }
    const execute = tool.execute;
    return {
      name,
      exposure: "agent" as const,
      inputSchema: z.unknown(),
      outputSchema: z.unknown(),
      idempotency: "none" as const,
      execute: async (context, actionInput) => execute(actionInput, ...(context.invocationArgs ?? [])),
      ...(input.reduceAction
        ? { reduce: (domainState: unknown, result: unknown) => input.reduceAction?.(domainState, name, result) }
        : {})
    };
  })
});
