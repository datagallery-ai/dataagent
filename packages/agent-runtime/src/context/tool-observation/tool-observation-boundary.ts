import { ContextRunState, type ContextRunIdentity } from "../inventory/context-run-state.js";
import { ContextBudgetAllocator } from "../policy/context-budget-allocator.js";
import { ContextPolicy } from "../policy/context-policy.js";
import type { ToolObservationAdapter } from "./tool-observation-adapter.js";
import { ToolObservationAdapterRegistry } from "./tool-observation-adapter-registry.js";
import {
  registerDefaultToolObservationAdapters
} from "./default-tool-observation-adapters.js";
import {
  DEFAULT_TOOL_OBSERVATION_SOURCE_LIMIT_PROFILES
} from "./tool-observation-budget-profile.js";
import { ToolObservationPackager } from "./tool-observation-packager.js";

export type CreateToolObservationBoundaryInput = {
  additionalAdapters?: ToolObservationAdapter[];
  identity: ContextRunIdentity;
  includeKnowledge?: boolean;
  mcpToolNames?: string[];
};

export type ToolObservationBoundary = {
  contextRunState: ContextRunState;
  packager: ToolObservationPackager;
};

export const createToolObservationBoundary = (
  input: CreateToolObservationBoundaryInput
): ToolObservationBoundary => {
  const budgetAllocator = new ContextBudgetAllocator({
    sourceLimitProfiles: DEFAULT_TOOL_OBSERVATION_SOURCE_LIMIT_PROFILES
  });
  const registry = new ToolObservationAdapterRegistry();
  const contextRunState = new ContextRunState(input.identity);
  const packager = new ToolObservationPackager(
    budgetAllocator,
    registry,
    new ContextPolicy(),
    contextRunState
  );

  registerDefaultToolObservationAdapters({
    includeKnowledge: Boolean(input.includeKnowledge),
    ...(input.mcpToolNames?.length ? { mcpToolNames: input.mcpToolNames } : {}),
    registry,
    ...(input.additionalAdapters ? { additionalAdapters: input.additionalAdapters } : {})
  });

  return {
    contextRunState,
    packager
  };
};
