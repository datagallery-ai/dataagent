import type { LiveRun, LiveToolCallRecord } from "./live-run-state.js";

/**
 * Connection status for the TUI client
 */
export type ConnectionStatus = "connected" | "disconnected" | "error";

/**
 * Message element - building blocks of a message
 */
export type MessageElement =
  | { type: 'text'; content: string; timestamp: number }
  | {
      type: 'reasoning';
      content: string;
      timestamp: number;
      isStreaming?: boolean | undefined;
    }
  | {
      type: 'tool_call';
      toolCallId: string;
      timestamp: number;
      runId?: string | undefined;
      toolCall?: LiveToolCallRecord | undefined;
    };

/**
 * Display message for chat history in TUI
 */
export interface DisplayMessage {
  id: string;
  role: "user" | "assistant" | "system";
  timestamp: number;
  /** For assistant messages that are still streaming */
  isStreaming?: boolean | undefined;
  /** Message elements (text, tool calls, etc.) in order */
  elements: MessageElement[];
}

/**
 * TUI session state extending LiveRun with TUI-specific fields
 */
export interface TuiSessionState extends LiveRun {
  /** Chat message history for display */
  messages: DisplayMessage[];
  /** Current user input buffer */
  inputBuffer: string;
  /** WebSocket/connection status */
  connectionStatus: ConnectionStatus;
  /** Current thread ID for the session */
  threadId?: string | undefined;
  /** Last error message if connectionStatus is 'error' */
  lastError?: string | undefined;
}

/**
 * Create initial TUI session state
 */
export function createInitialTuiState(): TuiSessionState {
  return {
    plan: [],
    events: [],
    artifacts: [],
    audits: [],
    runStatus: "idle",
    toolCalls: [],
    messages: [],
    inputBuffer: "",
    connectionStatus: "disconnected",
  };
}

/**
 * Update connection status
 */
export function updateConnectionStatus(
  state: TuiSessionState,
  status: ConnectionStatus,
  error?: string,
): TuiSessionState {
  const result: TuiSessionState = {
    ...state,
    connectionStatus: status,
  };

  if (status === "error" && error !== undefined) {
    result.lastError = error;
  } else if (status !== "error") {
    delete result.lastError;
  }

  return result;
}

/**
 * Update input buffer
 */
export function updateInputBuffer(
  state: TuiSessionState,
  buffer: string,
): TuiSessionState {
  return {
    ...state,
    inputBuffer: buffer,
  };
}

/**
 * Clear input buffer
 */
export function clearInputBuffer(state: TuiSessionState): TuiSessionState {
  return {
    ...state,
    inputBuffer: "",
  };
}

/**
 * Set thread ID
 */
export function setThreadId(
  state: TuiSessionState,
  threadId: string | null,
): TuiSessionState {
  return {
    ...state,
    threadId: threadId ?? undefined,
  };
}
