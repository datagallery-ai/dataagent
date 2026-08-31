import { EventType, type BaseEvent } from "@ag-ui/client";
import { randomUUID } from "node:crypto";

type PendingToolCall = {
  toolCallId: string;
  toolName: string;
  hasResult: boolean;
};

/**
 * Safety net for the TOOL_CALL_RESULT contract.
 *
 * The authoritative TOOL_CALL_RESULT is now emitted at the governed tool execution boundary
 * (see GovernedToolFactory), so every governed tool call produces a canonical result for both
 * success and failure. This bridge no longer backfills from ACTIVITY STEP snapshots; it only
 * tracks whether each TOOL_CALL_END received a result and, at run termination, emits a loud
 * TOOL_RESULT_NOT_DELIVERED marker for any tool that finished without one. That marker means a
 * contract gap that should be fixed at the boundary rather than papered over here.
 */
export class ToolCallResultBridge {
  private readonly pending: PendingToolCall[] = [];

  observe(event: BaseEvent): BaseEvent[] {
    if (event.type === EventType.TOOL_CALL_END && event.toolCallId) {
      this.pending.push({
        toolCallId: String(event.toolCallId),
        toolName: readString(event.toolCallName) ?? "unknown",
        hasResult: false
      });
    }

    if (event.type === EventType.TOOL_CALL_RESULT && event.toolCallId) {
      const entry = this.pending.find((item) => item.toolCallId === String(event.toolCallId));
      if (entry) entry.hasResult = true;
    }

    return [];
  }

  /** Must be delivered before RUN_FINISHED / RUN_ERROR — CopilotKit rejects late TOOL_CALL_RESULT. */
  flushPendingResults(): BaseEvent[] {
    const extras: BaseEvent[] = [];
    for (const pending of this.pending) {
      if (pending.hasResult) continue;
      extras.push(
        createToolCallResult(
          pending.toolCallId,
          pending.toolName,
          JSON.stringify({
            error: "TOOL_RESULT_NOT_DELIVERED",
            message:
              "Tool finished without an authoritative TOOL_CALL_RESULT from the execution boundary."
          })
        )
      );
      pending.hasResult = true;
    }
    return extras;
  }
}

const createToolCallResult = (
  toolCallId: string,
  toolCallName: string,
  content: string,
): BaseEvent => ({
  type: EventType.TOOL_CALL_RESULT,
  toolCallId,
  toolCallName,
  content,
  messageId: randomUUID(),
  role: "tool",
  timestamp: Date.now()
});

const readString = (value: unknown): string | undefined =>
  typeof value === "string" && value.length > 0 ? value : undefined;
