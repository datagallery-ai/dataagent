import { LibSQLStore } from "@mastra/libsql";
import { Memory } from "@mastra/memory";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { CONVERSATION_WORKING_MEMORY_CONFIG } from "./conversation-memory-bridge.js";

export const AGENT_MEMORY_MODES = ["off", "shadow", "working-memory-readonly"] as const;

export type AgentMemoryMode = typeof AGENT_MEMORY_MODES[number];

export type AgentMemoryRuntimeOptions = {
  conversationMemoryMode?: AgentMemoryMode | undefined;
};

export type TaskStateRuntime = {
  memory: Memory;
  storage: LibSQLStore;
  close(): Promise<void>;
};

export type AgentMemoryRuntime = TaskStateRuntime;

/** Create application-scoped Mastra storage used for controlled agent memory slices. */
export const createAgentMemoryRuntime = async (
  databasePath: string,
  options: AgentMemoryRuntimeOptions = {}
): Promise<AgentMemoryRuntime> => {
  const absolutePath = resolve(databasePath);
  mkdirSync(dirname(absolutePath), { recursive: true });

  const storage = new LibSQLStore({
    id: "data-foundry-task-state",
    url: `file:${absolutePath}`
  });
  await storage.init();

  const memory = new Memory({
    storage,
    vector: false,
    options: createAgentMemoryOptions(options.conversationMemoryMode)
  });

  return {
    memory,
    storage,
    close: () => storage.close()
  };
};

/** Create application-scoped Mastra storage used by durable task state and goal APIs. */
export const createTaskStateRuntime = async (
  databasePath: string,
  options: AgentMemoryRuntimeOptions = {}
): Promise<TaskStateRuntime> => createAgentMemoryRuntime(databasePath, options);

export const parseAgentMemoryMode = (
  value: string | undefined,
  fallback: AgentMemoryMode = "shadow"
): AgentMemoryMode => {
  if (value && AGENT_MEMORY_MODES.includes(value as AgentMemoryMode)) {
    return value as AgentMemoryMode;
  }
  return fallback;
};

const createAgentMemoryOptions = (mode: AgentMemoryMode | undefined) => ({
  generateTitle: false as const,
  lastMessages: false as const,
  observationalMemory: false as const,
  readOnly: true as const,
  semanticRecall: false as const,
  workingMemory: mode === "working-memory-readonly"
    ? CONVERSATION_WORKING_MEMORY_CONFIG.workingMemory
    : { enabled: false as const }
});
