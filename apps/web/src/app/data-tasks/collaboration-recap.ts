import type { Message } from "@ag-ui/core";
import type { LiveRun, LiveToolCallRecord } from "./live-run-state";
import type { CollaborationResponseRecord } from "./components/chat/collaboration-responses";

const COLLABORATION_TOOL_NAMES = new Set(["ask_user", "submit_plan"]);

type MessageLike = {
  id?: string;
  role?: string;
  toolCalls?: unknown[];
};

function toolCallIdOf(call: unknown): string | undefined {
  if (!call || typeof call !== "object") return undefined;
  const id = (call as Record<string, unknown>).id;
  return typeof id === "string" ? id : undefined;
}

function messageHasToolCall(message: MessageLike, toolCallId: string): boolean {
  if (!Array.isArray(message.toolCalls)) return false;
  return message.toolCalls.some((call) => toolCallIdOf(call) === toolCallId);
}

export function hasCollaborationResponse(
  toolCallId: string,
  responses: CollaborationResponseRecord[],
): boolean {
  return responses.some((response) => response.toolCallId === toolCallId);
}

export function isCollaborationToolCallAnswered(
  toolCall: LiveToolCallRecord,
  responses: CollaborationResponseRecord[],
): boolean {
  if (hasCollaborationResponse(toolCall.id, responses)) {
    return true;
  }
  return toolCall.status === "success" || toolCall.status === "failed";
}

function currentRunToolCalls(liveRun: LiveRun): LiveToolCallRecord[] {
  const historyEndIndex = liveRun.runHistory?.at(-1)?.toolCallEndIndex ?? 0;
  return liveRun.toolCalls.slice(historyEndIndex);
}

export function findPendingCollaborationToolCall(
  liveRun: LiveRun | null,
  responses: CollaborationResponseRecord[],
  runStatus: LiveRun["runStatus"] = liveRun?.runStatus ?? "idle",
): LiveToolCallRecord | undefined {
  if (!liveRun || runStatus !== "suspended") {
    return undefined;
  }

  const segment = currentRunToolCalls(liveRun);
  for (let index = segment.length - 1; index >= 0; index -= 1) {
    const call = segment[index];
    if (!call || !COLLABORATION_TOOL_NAMES.has(call.name)) {
      continue;
    }
    if (!isCollaborationToolCallAnswered(call, responses)) {
      return call;
    }
  }
  return undefined;
}

export function reconcileSuspendedLiveRunState(
  liveRun: LiveRun,
  responses: CollaborationResponseRecord[],
): LiveRun {
  if (liveRun.runStatus !== "suspended") {
    return liveRun;
  }
  // HITL suspend can arrive before TOOL_CALL_* events land in LiveRun.
  if (currentRunToolCalls(liveRun).length === 0) {
    return liveRun;
  }
  if (findPendingCollaborationToolCall(liveRun, responses, liveRun.runStatus)) {
    return liveRun;
  }
  return {
    ...liveRun,
    runStatus: "completed",
    toolCalls: liveRun.toolCalls.map((call) =>
      call.status === "running" ? { ...call, status: "success" as const } : call,
    ),
  };
}

/** Resume gate for pending interactions restored purely from REST metadata. */
export function canResumeRestoredInteraction(input: {
  toolCallId?: string;
  collaborationResponses: CollaborationResponseRecord[];
}): boolean {
  if (!input.toolCallId) {
    return false;
  }
  return !hasCollaborationResponse(input.toolCallId, input.collaborationResponses);
}

export function resolveCollaborationRecapAnchorMessageId(
  response: CollaborationResponseRecord,
  messages: MessageLike[],
): string | undefined {
  if (response.assistantMessageId) {
    if (messages.some((item) => item.id === response.assistantMessageId)) {
      return response.assistantMessageId;
    }
  }

  const withToolCall = messages.filter(
    (item) => item.role === "assistant" && messageHasToolCall(item, response.toolCallId),
  );
  if (withToolCall.length > 0) {
    if (response.assistantMessageId) {
      const preferred = withToolCall.find((item) => item.id === response.assistantMessageId);
      if (preferred?.id) {
        return preferred.id;
      }
    }
    return withToolCall[0]?.id;
  }

  return undefined;
}

export function shouldShowCollaborationRecapOnMessage(
  message: MessageLike,
  response: CollaborationResponseRecord,
  messages: MessageLike[],
): boolean {
  const anchorId = resolveCollaborationRecapAnchorMessageId(response, messages);
  return Boolean(anchorId && message.id === anchorId);
}

/** Anchor assistant message for an in-flight HITL interrupt (live or restored). */
export function resolvePendingInterruptAnchorMessageId(
  toolCallId: string,
  messages: MessageLike[],
  liveRun: LiveRun | null,
  liveRunStatus: LiveRun["runStatus"],
): string | undefined {
  const withToolCall = messages.filter(
    (item) => item.role === "assistant" && messageHasToolCall(item, toolCallId),
  );
  if (withToolCall.length > 0) {
    return withToolCall[withToolCall.length - 1]?.id;
  }

  if (liveRunStatus === "suspended" && liveRun?.toolCalls.some((call) => call.id === toolCallId)) {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index]?.role === "assistant") {
        return messages[index]?.id;
      }
    }
    return messages[messages.length - 1]?.id;
  }

  return undefined;
}

export function shouldShowPendingInterruptOnMessage(
  message: MessageLike,
  toolCallId: string,
  messages: MessageLike[],
  liveRun: LiveRun | null,
  liveRunStatus: LiveRun["runStatus"],
): boolean {
  const anchorId = resolvePendingInterruptAnchorMessageId(
    toolCallId,
    messages,
    liveRun,
    liveRunStatus,
  );
  return Boolean(anchorId && message.id === anchorId);
}

/** Whether this message should host the inline HITL option card. */
export function messageHostsPendingCollaborationSlot(
  message: MessageLike,
  toolCallId: string | undefined,
  messages: MessageLike[],
  liveRun: LiveRun | null,
  liveRunStatus: LiveRun["runStatus"],
): boolean {
  if (!toolCallId || !message.id) {
    return false;
  }
  if (
    shouldShowPendingInterruptOnMessage(
      message,
      toolCallId,
      messages,
      liveRun,
      liveRunStatus,
    )
  ) {
    return true;
  }
  return messages[messages.length - 1]?.id === message.id;
}

export function canResumeCollaborationInterrupt(input: {
  toolCallId?: string;
  collaborationResponses: CollaborationResponseRecord[];
  liveRun: LiveRun | null;
  liveRunStatus: LiveRun["runStatus"];
}): boolean {
  const { toolCallId, collaborationResponses, liveRun, liveRunStatus } = input;
  if (!toolCallId) {
    return false;
  }
  if (hasCollaborationResponse(toolCallId, collaborationResponses)) {
    return false;
  }

  const liveCall = liveRun?.toolCalls.find((call) => call.id === toolCallId);
  if (liveCall && isCollaborationToolCallAnswered(liveCall, collaborationResponses)) {
    return false;
  }

  if (liveRunStatus === "running") {
    if (liveCall) {
      if (!COLLABORATION_TOOL_NAMES.has(liveCall.name)) {
        return false;
      }
      return liveCall.status === "running";
    }
    // on_interrupt can arrive before TOOL_CALL_* events are reflected in LiveRun.
    return true;
  }

  if (liveRunStatus !== "suspended") {
    return false;
  }

  const pending = findPendingCollaborationToolCall(
    liveRun,
    collaborationResponses,
    liveRunStatus,
  );
  return pending?.id === toolCallId;
}

export function shouldShowCollaborationRecap(
  _message: Message,
  _response: CollaborationResponseRecord,
  _liveRun: LiveRun | null,
): boolean {
  return true;
}
