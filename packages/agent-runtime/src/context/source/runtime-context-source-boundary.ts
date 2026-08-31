import {
  LongTermMemoryContextSource,
  type LongTermMemoryContextSourceOptions
} from "./long-term-memory-context-source.js";
import type { RuntimeContextSource } from "./runtime-context-source.js";
import { RuntimeContextSourceRegistry } from "./runtime-context-source-registry.js";
import {
  WorkingMemoryProjectionContextSource,
  type WorkingMemoryProjectionReader
} from "./working-memory-projection-context-source.js";

export type CreateDefaultRuntimeContextSourceRegistryInput = {
  additionalSources?: RuntimeContextSource[];
  longTermMemory?: LongTermMemoryContextSourceOptions;
  workingMemory?: WorkingMemoryProjectionReader;
};

export const createDefaultRuntimeContextSourceRegistry = (
  input: CreateDefaultRuntimeContextSourceRegistryInput = {}
): RuntimeContextSourceRegistry => {
  const registry = new RuntimeContextSourceRegistry();

  if (input.longTermMemory?.records.length) {
    registry.register(new LongTermMemoryContextSource(input.longTermMemory));
  }
  if (input.workingMemory) {
    registry.register(new WorkingMemoryProjectionContextSource({
      memory: input.workingMemory
    }));
  }
  input.additionalSources?.forEach((source) => registry.register(source));

  return registry;
};
