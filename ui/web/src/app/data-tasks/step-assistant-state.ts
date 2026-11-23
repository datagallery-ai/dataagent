import { findPendingCollaborationToolCall } from "./collaboration-recap";
import type { LiveRun, LiveRunStatus } from "./live-run-state";
import type { CollaborationResponseRecord } from "./components/chat/collaboration-responses";

export const COLLABORATION_TOOL_NAMES = new Set(["ask_user", "submit_plan"]);

type MessageLike = {
  id?: string;
  role?: string;
  toolCalls?: unknown[];
  content?: unknown;
};

function toolCallRecord(call: unknown): { id?: string; function?: { name?: string } } {
  if (!call || typeof call !== "object") return {};
  const record = call as Record<string, unknown>;
  const fn =
    record.function && typeof record.function === "object"
      ? (record.function as Record<string, unknown>)
      : {};
  return {
    id: typeof record.id === "string" ? record.id : undefined,
    function: {
      name: typeof fn.name === "string" ? fn.name : undefined,
    },
  };
}

export type StepAssistantFlags = {
  hasToolCalls: boolean;
  isLast: boolean;
  isLastAssistantInRun: boolean;
  isWaitingForUser: boolean;
  isCollaborationStep: boolean;
  isCollaborationComplete: boolean;
  /** Post-resume text on the same message as a completed HITL tool renders as an answer, not in the step card. */
  isCollaborationFollowUpAnswer: boolean;
  isFollowUpAnswerActive: boolean;
  isActive: boolean;
  isFinalAnswer: boolean;
  isFinalAnswerComplete: boolean;
  isThought: boolean;
  linkedCollaboration?: CollaborationResponseRecord;
};

export function messageHasToolCall(message: MessageLike, toolCallId: string): boolean {
  if (!Array.isArray(message.toolCalls)) return false;
  return message.toolCalls.some((call) => toolCallRecord(call).id === toolCallId);
}

export function hasLaterAssistantMessage(
  messageId: string | undefined,
  messages: MessageLike[],
): boolean {
  if (!messageId) return false;
  const index = messages.findIndex((item) => item.id === messageId);
  if (index < 0) return false;
  return messages.slice(index + 1).some((item) => item.role === "assistant");
}

export function shouldHideProcessStepForTimelineCollapse(input: {
  isProcessStep: boolean;
  timelineCollapsed: boolean;
}): boolean {
  return input.isProcessStep && input.timelineCollapsed;
}

function getRunMessageBounds(
  messages: MessageLike[],
  messageIndex: number,
): { lastUserIndex: number; nextUserIndex: number } {
  const lastUserIndex =
    messageIndex >= 0
      ? (messages
          .slice(0, messageIndex + 1)
          .map((item, index) => ({ item, index }))
          .filter(({ item }) => item.role === "user")
          .at(-1)?.index ?? -1)
      : -1;
  const nextUserIndex =
    messageIndex >= 0
      ? messages.findIndex((item, index) => index > messageIndex && item.role === "user")
      : -1;
  return { lastUserIndex, nextUserIndex };
}

function getRunSlice(messages: MessageLike[], messageIndex: number): MessageLike[] {
  if (messageIndex < 0) return messages;
  const { lastUserIndex, nextUserIndex } = getRunMessageBounds(messages, messageIndex);
  return messages.slice(lastUserIndex + 1, nextUserIndex > -1 ? nextUserIndex : undefined);
}

function hasLaterAssistantMessageInRun(
  messageId: string | undefined,
  messages: MessageLike[],
): boolean {
  if (!messageId) return false;
  const index = messages.findIndex((item) => item.id === messageId);
  if (index < 0) return false;
  const runSlice = getRunSlice(messages, index);
  const runIndex = runSlice.findIndex((item) => item.id === messageId);
  if (runIndex < 0) return false;
  return runSlice.slice(runIndex + 1).some((item) => item.role === "assistant");
}

function isLastAssistantMessageInRun(
  messageId: string | undefined,
  messages: MessageLike[],
): boolean {
  if (!messageId) return false;
  const index = messages.findIndex((item) => item.id === messageId);
  if (index < 0) return false;
  const runSlice = getRunSlice(messages, index);
  const assistants = runSlice.filter((item) => item.role === "assistant");
  if (assistants.length === 0) return false;
  return assistants[assistants.length - 1]?.id === messageId;
}

function laterAssistantHasCollaborationToolCallInRun(
  messageId: string | undefined,
  messages: MessageLike[],
): boolean {
  if (!messageId) return false;
  const index = messages.findIndex((item) => item.id === messageId);
  if (index < 0) return false;
  const runSlice = getRunSlice(messages, index);
  const runIndex = runSlice.findIndex((item) => item.id === messageId);
  if (runIndex < 0) return false;
  return runSlice.slice(runIndex + 1).some((item) => {
    if (item.role !== "assistant" || !Array.isArray(item.toolCalls)) {
      return false;
    }
    return item.toolCalls.some((call) =>
      COLLABORATION_TOOL_NAMES.has(toolCallRecord(call).function?.name ?? ""),
    );
  });
}

function laterAssistantHasAnsweredCollaborationInRun(
  messageId: string | undefined,
  messages: MessageLike[],
  responses: CollaborationResponseRecord[],
): boolean {
  if (!messageId) return false;
  const index = messages.findIndex((item) => item.id === messageId);
  if (index < 0) return false;
  const runSlice = getRunSlice(messages, index);
  const runIndex = runSlice.findIndex((item) => item.id === messageId);
  if (runIndex < 0) return false;
  return runSlice.slice(runIndex + 1).some((item) => {
    if (item.role !== "assistant") return false;
    return Boolean(findLinkedCollaborationResponse(item, messages, responses));
  });
}

export function laterAssistantHasCollaborationToolCall(
  messageId: string | undefined,
  messages: MessageLike[],
): boolean {
  return laterAssistantHasCollaborationToolCallInRun(messageId, messages);
}

export function inferCollaborationFromLiveRun(
  message: MessageLike,
  messages: MessageLike[],
  liveRun: LiveRun | null,
): boolean {
  return liveRunCollaborationCandidate(message, messages, liveRun) !== undefined;
}

function liveRunCandidateRange(
  message: MessageLike,
  messages: MessageLike[],
  liveRun: LiveRun | null,
): { start: number; end: number } | null {
  if (!liveRun) return null;

  const assistants = messages.filter((item) => item.role === "assistant");
  const assistantIndex = assistants.findIndex((item) => item.id === message.id);
  if (assistantIndex < 0) return null;

  let liveToolIndex = 0;
  for (let index = 0; index < assistantIndex; index += 1) {
    const toolCount = assistants[index].toolCalls?.length ?? 0;
    liveToolIndex += toolCount > 0 ? toolCount : 1;
  }

  const currentToolCount = message.toolCalls?.length ?? 0;
  const sliceEnd = liveToolIndex + (currentToolCount > 0 ? currentToolCount : 1);
  return { start: liveToolIndex, end: sliceEnd };
}

export function resolveAssistantLiveToolCalls(input: {
  message: MessageLike;
  messages: MessageLike[];
  liveRun: LiveRun | null;
}): LiveRun["toolCalls"] {
  const { message, messages, liveRun } = input;
  if (!liveRun) return [];

  const toolCalls = Array.isArray(message.toolCalls) ? message.toolCalls : [];
  if (toolCalls.length > 0) {
    const liveById = new Map(liveRun.toolCalls.map((call) => [call.id, call]));
    return toolCalls
      .map((call) => {
        const id = toolCallRecord(call).id;
        return id ? liveById.get(id) : undefined;
      })
      .filter((call): call is LiveRun["toolCalls"][number] => Boolean(call));
  }

  if (!isLastAssistantMessageInRun(message.id, messages)) return [];

  const range = liveRunCandidateRange(message, messages, liveRun);
  if (!range) return [];
  return liveRun.toolCalls
    .slice(range.start, range.end)
    .filter((call) => call.status === "running");
}

function liveRunCollaborationCandidate(
  message: MessageLike,
  messages: MessageLike[],
  liveRun: LiveRun | null,
): { index: number; name: string } | undefined {
  const range = liveRunCandidateRange(message, messages, liveRun);
  if (!range || !liveRun) return undefined;
  const candidates = liveRun.toolCalls.slice(range.start, range.end);
  const relativeIndex = candidates.findIndex((call) => COLLABORATION_TOOL_NAMES.has(call.name));
  if (relativeIndex < 0) return undefined;
  const call = candidates[relativeIndex];
  return { index: range.start + relativeIndex, name: call.name };
}

export function findLinkedCollaborationResponse(
  message: MessageLike,
  _messages: MessageLike[],
  responses: CollaborationResponseRecord[],
): CollaborationResponseRecord | undefined {
  if (!message.id) return undefined;

  return responses.find(
    (response) =>
      response.assistantMessageId === message.id ||
      messageHasToolCall(message, response.toolCallId),
  );
}

export function resolveStepAssistantFlags(input: {
  message: MessageLike;
  messages: MessageLike[];
  content: string;
  isRunning: boolean;
  liveRunStatus: LiveRunStatus;
  liveRun: LiveRun | null;
  collaborationResponses: CollaborationResponseRecord[];
}): StepAssistantFlags {
  const { message, messages, content, isRunning, liveRunStatus, liveRun, collaborationResponses } =
    input;
  const toolCalls = Array.isArray(message.toolCalls) ? message.toolCalls : [];
  const liveToolCalls = resolveAssistantLiveToolCalls({ message, messages, liveRun });
  const hasToolCalls = toolCalls.length > 0 || liveToolCalls.length > 0;
  const rawToolNames =
    toolCalls.length > 0
      ? toolCalls.map((call) => toolCallRecord(call).function?.name ?? "")
      : liveToolCalls.map((call) => call.name);
  const hasNamedToolCalls = rawToolNames.some((name) => name.trim().length > 0);
  const isLastInThread = messages[messages.length - 1]?.id === message.id;
  const isLastAssistantInRun = isLastAssistantMessageInRun(message.id, messages);
  const pendingCollaboration = findPendingCollaborationToolCall(
    liveRun,
    collaborationResponses,
    liveRunStatus,
  );
  const pendingToolCallId = pendingCollaboration?.id;
  const threadHasPendingToolCallMessage =
    pendingToolCallId !== undefined &&
    messages.some(
      (item) =>
        item.role === "assistant" && messageHasToolCall(item, pendingToolCallId),
    );
  const isWaitingForUser =
    liveRunStatus === "suspended" &&
    pendingCollaboration !== undefined &&
    (messageHasToolCall(message, pendingCollaboration.id) ||
      (!threadHasPendingToolCallMessage && isLastInThread));
  const hasLaterAssistantInRun = hasLaterAssistantMessageInRun(message.id, messages);
  const orphanPreambleCandidate =
    !hasToolCalls && content.length > 0 && hasLaterAssistantInRun;
  const canInferCollaborationFromLiveRun =
    !hasNamedToolCalls &&
    !orphanPreambleCandidate &&
    !laterAssistantHasCollaborationToolCallInRun(message.id, messages) &&
    !laterAssistantHasAnsweredCollaborationInRun(message.id, messages, collaborationResponses) &&
    (content.length === 0 || hasLaterAssistantInRun) &&
    (liveRunStatus !== "suspended" || isLastInThread);
  const linkedCollaboration = findLinkedCollaborationResponse(
    message,
    messages,
    collaborationResponses,
  );
  const isCollaborationComplete = Boolean(linkedCollaboration);
  const isCollaborationFollowUpAnswer =
    isCollaborationComplete &&
    hasToolCalls &&
    rawToolNames.some((name) => COLLABORATION_TOOL_NAMES.has(name)) &&
    content.length > 0;
  const isCollaborationStep =
    !isCollaborationComplete &&
    (rawToolNames.some((name) => COLLABORATION_TOOL_NAMES.has(name)) ||
      (canInferCollaborationFromLiveRun &&
        inferCollaborationFromLiveRun(message, messages, liveRun)) ||
      (isWaitingForUser && isLastInThread));
  const orphanPreamble = orphanPreambleCandidate;
  const isFollowUpAnswerActive =
    isCollaborationFollowUpAnswer &&
    isLastInThread &&
    !!isRunning &&
    liveRunStatus === "running";
  const isActive =
    isLastInThread &&
    liveRunStatus === "running" &&
    !!isRunning &&
    !isCollaborationComplete &&
    !orphanPreamble &&
    !isWaitingForUser &&
    !isCollaborationStep;
  const isFinalAnswer =
    isLastAssistantInRun &&
    content.length > 0 &&
    !hasToolCalls &&
    !isWaitingForUser &&
    !isCollaborationStep &&
    !isCollaborationComplete &&
    !orphanPreamble &&
    !isCollaborationFollowUpAnswer;
  const isFinalAnswerComplete = isFinalAnswer && !isActive;
  const isThought =
    orphanPreamble ||
    (!hasToolCalls &&
      !isFinalAnswer &&
      !isCollaborationStep &&
      !isCollaborationFollowUpAnswer &&
      content.length > 0);

  return {
    hasToolCalls,
    isLast: isLastInThread,
    isLastAssistantInRun,
    isWaitingForUser,
    isCollaborationStep,
    isCollaborationComplete,
    isCollaborationFollowUpAnswer,
    isFollowUpAnswerActive,
    isActive,
    isFinalAnswer,
    isFinalAnswerComplete,
    isThought,
    linkedCollaboration,
  };
}

function liveRunToolOffsetBeforeRun(messages: MessageLike[], lastUserIndex: number): number {
  const assistants = messages
    .slice(0, Math.max(0, lastUserIndex + 1))
    .filter((item) => item.role === "assistant");
  let liveToolIndex = 0;
  for (const item of assistants) {
    const toolCount = item.toolCalls?.length ?? 0;
    liveToolIndex += toolCount > 0 ? toolCount : 1;
  }
  return liveToolIndex;
}

export function resolveAssistantToolStepNumber(input: {
  message: MessageLike;
  messages: MessageLike[];
  liveRun: LiveRun | null;
  collaborationResponses: CollaborationResponseRecord[];
}): number {
  const { message, messages, liveRun, collaborationResponses } = input;
  const messageIndex = messages.findIndex((item) => item.id === message.id);
  const { lastUserIndex, nextUserIndex } = getRunMessageBounds(messages, messageIndex);
  const runMessages =
    messageIndex >= 0
      ? messages.slice(lastUserIndex + 1, nextUserIndex > -1 ? nextUserIndex : undefined)
      : messages;

  const toolCalls = Array.isArray(message.toolCalls) ? message.toolCalls : [];
  if (toolCalls.length > 0) {
    const processSteps = runMessages.filter(
      (item) =>
        item.role === "assistant" &&
        Array.isArray(item.toolCalls) &&
        item.toolCalls.length > 0,
    );
    const index = processSteps.findIndex((item) => item.id === message.id);
    return index >= 0 ? index + 1 : 0;
  }

  const linkedCollaboration = findLinkedCollaborationResponse(
    message,
    messages,
    collaborationResponses,
  );
  if (linkedCollaboration && liveRun) {
    const liveToolIndex = liveRun.toolCalls.findIndex(
      (call) => call.id === linkedCollaboration.toolCallId,
    );
    if (liveToolIndex >= 0) {
      const offset = liveRunToolOffsetBeforeRun(messages, lastUserIndex);
      return liveToolIndex - offset + 1;
    }
  }

  const inferred = liveRunCollaborationCandidate(message, messages, liveRun);
  if (inferred) {
    const offset = liveRunToolOffsetBeforeRun(messages, lastUserIndex);
    return inferred.index - offset + 1;
  }
  return 0;
}
