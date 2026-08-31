import type { LiveToolCallRecord } from "./live-run-state";

export type CopilotToolStatus = "inProgress" | "executing" | "complete";
export type BackendToolPhase = "running" | "success" | "failed";
export type ToolDisplayStatus = "pending" | "executing" | "complete" | "failed";

/** Merge CopilotKit message state with AG-UI backend tool events. */
export function resolveToolDisplayStatus(input: {
  copilotStatus: CopilotToolStatus;
  backendPhase?: BackendToolPhase;
  hasResult: boolean;
  resultIsError?: boolean;
}): ToolDisplayStatus {
  if (input.hasResult) {
    return input.resultIsError ? "failed" : "complete";
  }
  if (input.backendPhase === "failed") return "failed";
  if (input.backendPhase === "success") return "complete";
  if (input.copilotStatus === "complete") return "failed";
  if (input.copilotStatus === "executing" || input.backendPhase === "running") {
    return "executing";
  }
  return "pending";
}

export type ToolResultErrorKind = "tool" | "protocol" | "delivery";

export type ParsedToolResultError = {
  kind: ToolResultErrorKind;
  title: string;
  message: string;
  hint?: string;
};

export function parseToolResultError(result?: string): ParsedToolResultError | null {
  if (!result) return null;
  try {
    const parsed = JSON.parse(result) as {
      status?: unknown;
      reason?: unknown;
      error?: unknown;
      message?: unknown;
    };

    if (parsed.status === "error") {
      if (parsed.reason === "missing_terminal_event") {
        return {
          kind: "protocol",
          title: "Result sync failed",
          message: "The tool result arrived after the chat run ended, so the frontend could not receive the observation.",
          hint: "The SQL may have run on the backend. Check the right-side full trace, then retry this question.",
        };
      }
      return {
        kind: "protocol",
        title: "Result sync failed",
        message:
          typeof parsed.message === "string" && parsed.message.trim()
            ? parsed.message
            : "The tool observation could not be written to the chat thread.",
      };
    }

    if (parsed.error === "TOOL_RESULT_NOT_DELIVERED") {
      return {
        kind: "delivery",
        title: "Result not delivered",
        message:
          typeof parsed.message === "string" && parsed.message.trim()
            ? parsed.message
            : "The backend did not push TOOL_CALL_RESULT in the AG-UI stream.",
        hint: "Check the SQL audit or step status in the right-side trace panel.",
      };
    }

    if (parsed.error !== undefined) {
      return {
        kind: "tool",
        title: "Tool execution failed",
        message:
          typeof parsed.message === "string" && parsed.message.trim()
            ? parsed.message
            : typeof parsed.error === "string"
              ? parsed.error
              : "Tool execution failed.",
      };
    }
  } catch {
    return null;
  }
  return null;
}

export function toolResultLooksLikeError(result?: string): boolean {
  return parseToolResultError(result) !== null;
}

export function resolveToolFailurePresentation(result?: string): ParsedToolResultError {
  const parsed = parseToolResultError(result);
  if (parsed) return parsed;
  if (!result) {
    return {
      kind: "delivery",
      title: "Result not delivered",
      message: "The backend did not return an observation.",
    };
  }
  return {
    kind: "tool",
    title: "Tool execution failed",
    message: result.length > 240 ? `${result.slice(0, 240)}…` : result,
  };
}

export function toolDisplayStatusLabel(status: ToolDisplayStatus): string {
  switch (status) {
    case "complete":
      return "Completed";
    case "executing":
      return "Running";
    case "failed":
      return "Failed";
    default:
      return "Pending";
  }
}

export function toolPendingHint(status: ToolDisplayStatus): string {
  switch (status) {
    case "executing":
      return "The tool is running. Waiting for the backend result.";
    case "failed":
      return "Tool execution failed. Check the chat or retry.";
    default:
      return "This tool call is planned and waiting to start.";
  }
}

export function buildBackendToolPhaseMap(
  toolCalls: LiveToolCallRecord[],
): ReadonlyMap<string, BackendToolPhase> {
  return new Map(toolCalls.map((call) => [call.id, call.status]));
}

export function buildBackendToolResultMap(
  toolCalls: LiveToolCallRecord[],
): ReadonlyMap<string, string> {
  const map = new Map<string, string>();
  for (const call of toolCalls) {
    if (call.result) map.set(call.id, call.result);
  }
  return map;
}
