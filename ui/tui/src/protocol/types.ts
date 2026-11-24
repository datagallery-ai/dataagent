import type {
  Context,
  Message,
  ResumeEntry,
  Tool,
} from "@ag-ui/core";
import type { AgentEvent } from "./agui-events.js";

export type AgentMessage = Message;
export type AgentContext = Context;
export type CopilotKitEvent = AgentEvent;

export interface RunAgentInput {
  answers?: Record<string, string> | undefined;
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
  /** Abort one in-flight stream. A later run keeps its own controller. */
  cancelRun?(runId: string): void;
}

export interface CopilotKitError extends Error {
  code?: string | undefined;
  statusCode?: number | undefined;
}
