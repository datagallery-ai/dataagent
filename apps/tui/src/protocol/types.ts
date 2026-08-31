import type {
  BaseEvent,
  Context,
  Message,
  ResumeEntry,
  Tool,
} from "@ag-ui/core";

export type AgentMessage = Message;
export type AgentContext = Context;
export type CopilotKitEvent = BaseEvent;

export interface RunAgentInput {
  threadId: string;
  runId: string;
  parentRunId?: string | undefined;
  messages: AgentMessage[];
  tools?: Tool[] | undefined;
  context?: AgentContext[] | undefined;
  state?: unknown;
  forwardedProps?: Record<string, unknown> | undefined;
  resume?: ResumeEntry[] | undefined;
}

export interface AgentClient {
  runAgent(input: RunAgentInput): AsyncGenerator<CopilotKitEvent>;
}

export interface CopilotKitError extends Error {
  code?: string | undefined;
  statusCode?: number | undefined;
}
