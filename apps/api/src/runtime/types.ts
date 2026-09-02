import type { Message } from "@ag-ui/client";

export const RUNTIME_PROVIDER = "deepagents";
export const RUNTIME_CONTRACT_VERSION = "v1";
export const RUNTIME_BOUND_EVENT = "runtime.bound";
export const INTERRUPT_EVENT_NAME = "on_interrupt";
export const AGENT_INTERRUPT_TYPE = "agent_interrupt";
export const LEGACY_MASTRA_INTERRUPT_TYPE = "mastra_suspend";

export const V1_SYSTEM_PROMPT = [
  "You are DataFoundry's assistant.",
  "Data warehouse tools, knowledge retrieval, and skill packages are not connected in this runtime version.",
  "You may converse, use built-in planning/todo/filesystem tools, and ask the user questions when you need confirmation."
].join(" ");

export type RuntimeInterruptToolName = "ask_user" | "submit_plan";

export type RuntimeInterrupt = {
  type: typeof AGENT_INTERRUPT_TYPE | typeof LEGACY_MASTRA_INTERRUPT_TYPE;
  args?: unknown;
  resumeSchema?: unknown;
  runId: string;
  suspendPayload?: unknown;
  toolCallId: string;
  toolName: RuntimeInterruptToolName;
};

export type RuntimeRunResume = {
  interrupt: RuntimeInterrupt;
  response: unknown;
};

export type RuntimeModelRef = {
  name?: string;
  profileId?: string;
  provider?: string;
};

export type RuntimeRunRequest = {
  checkpointRef?: string;
  limits?: { maxSteps?: number };
  messages: Message[];
  model?: RuntimeModelRef;
  resume?: RuntimeRunResume;
  runId: string;
  systemPrompt: string;
  threadId: string;
  trace?: {
    userId?: string;
    workspaceId?: string;
  };
};

export type RuntimeCapabilities = {
  cancel: boolean;
  interrupt: boolean;
  streaming: boolean;
  tools: boolean;
};

export type RuntimeHealth = {
  capabilities: RuntimeCapabilities;
  provider: string;
  status: "ok" | "degraded" | "unavailable";
  version: string;
};

export type RuntimeBoundValue = {
  checkpointRef?: string;
  provider: string;
  version: string;
};

export type RuntimeTransport = {
  cancelRun(runId: string, reason?: string): Promise<void>;
  health(): Promise<RuntimeHealth>;
  startRun(
    request: RuntimeRunRequest,
    options?: { signal?: AbortSignal }
  ): AsyncIterable<unknown>;
};
