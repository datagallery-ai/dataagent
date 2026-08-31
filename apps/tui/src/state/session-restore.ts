import type {
  ConversationMessage,
  SessionArtifact,
  ConversationToolCall,
  SessionConversation,
} from "../config/index.js";
import type { DataArtifact } from "./data-task-state.js";
import { dataArtifactFromArtifactValue } from "./live-run-state.js";
import type { LiveToolCallRecord } from "./live-run-state.js";
import type { DisplayMessage, MessageElement } from "./tui-state.js";

export type RestoredSessionConversation = {
  threadId: string;
  title?: string | undefined;
  messages: DisplayMessage[];
  toolCalls: LiveToolCallRecord[];
  artifacts: DataArtifact[];
};

export function restoreSessionConversation(
  dto: SessionConversation,
  artifacts: SessionArtifact[] = [],
): RestoredSessionConversation {
  return {
    threadId: dto.sessionId,
    ...(dto.title ? { title: dto.title } : {}),
    messages: conversationToDisplayMessages(dto),
    toolCalls: conversationToToolCalls(dto.toolCalls),
    artifacts: sessionArtifactsToDataArtifacts(artifacts),
  };
}

export function sessionArtifactsToDataArtifacts(artifacts: SessionArtifact[]): DataArtifact[] {
  return [...artifacts].reverse().map(sessionArtifactToDataArtifact);
}

function sessionArtifactToDataArtifact(artifact: SessionArtifact): DataArtifact {
  const dataArtifact = dataArtifactFromArtifactValue({
    id: artifact.id,
    type: artifact.type,
    name: artifact.name,
    title: artifact.name,
    ...(artifact.fileId ? { file_id: artifact.fileId } : {}),
    ...(artifact.downloadUrl ? { download_url: artifact.downloadUrl } : {}),
    ...(artifact.mimeType ? { mimeType: artifact.mimeType } : {}),
    ...(artifact.preview_json !== undefined ? { preview_json: artifact.preview_json } : {}),
    ...(artifact.preview_available !== undefined ? { preview_available: artifact.preview_available } : {}),
    ...(artifact.runId ? { run_id: artifact.runId } : {}),
    ...(artifact.toolCallId ? { tool_call_id: artifact.toolCallId } : {}),
    ...(artifact.stepId ? { step_id: artifact.stepId } : {}),
  });
  return {
    ...dataArtifact,
    ...(artifact.createdAt ? { recordedAtMs: timestampFromIso(artifact.createdAt, Date.now()) } : {}),
  };
}

/**
 * Map a persisted conversation to the same interleaved layout the live run path
 * produces: one assistant block per run whose elements alternate between text
 * segments and the tool calls each segment triggered, ordered chronologically.
 */
export function conversationToDisplayMessages(
  dto: SessionConversation,
): DisplayMessage[] {
  const sortedMessages = [...dto.messages].sort(
    (left, right) => left.position - right.position,
  );
  const toolCallsByRun = groupToolCallsByRun(dto.toolCalls);
  // The live path stamps an assistant turn when its run starts (i.e. the user
  // message time), so reuse that here instead of the run-end flush timestamps
  // carried by the persisted assistant messages.
  const runStartTimestamps = userTimestampsByRun(sortedMessages);

  const messages: DisplayMessage[] = [];
  let index = 0;

  while (index < sortedMessages.length) {
    const entry = sortedMessages[index];

    if (entry.role !== "assistant") {
      const content = displayTextForEntry(entry);
      if (content) {
        const timestamp = timestampFromIso(entry.createdAt, Date.now());
        messages.push({
          id: entry.messageId ?? entry.id,
          role: entry.role,
          timestamp,
          elements: [{ type: "text", content, timestamp }],
        });
      }
      index += 1;
      continue;
    }

    // Merge the contiguous block of assistant messages sharing this runId into a
    // single turn, matching the live path's single streaming assistant message.
    const runId = entry.runId;
    const runEntries: ConversationMessage[] = [];
    while (
      index < sortedMessages.length &&
      sortedMessages[index].role === "assistant" &&
      sortedMessages[index].runId === runId
    ) {
      runEntries.push(sortedMessages[index]);
      index += 1;
    }

    const block = buildAssistantRunBlock(
      runId,
      runEntries,
      toolCallsByRun.get(runId) ?? [],
      runStartTimestamps.get(runId),
    );
    if (block) {
      messages.push(block);
    }
  }

  return messages;
}

type AssistantSegment = { entry: ConversationMessage; elements: MessageElement[] };

/**
 * Build a single assistant turn for one run, interleaving each text segment with
 * the tool calls it triggered (`text, tools, text, tools, ...`).
 */
function buildAssistantRunBlock(
  runId: string,
  runEntries: ConversationMessage[],
  runToolCalls: ConversationToolCall[],
  runStartTimestamp: number | undefined,
): DisplayMessage | undefined {
  const fallbackTimestamp = timestampFromIso(runEntries[0]?.createdAt, Date.now());
  const timestamp = runStartTimestamp ?? fallbackTimestamp;
  const segments: AssistantSegment[] = runEntries
    .map((entry) => ({ entry, elements: assistantElementsForEntry(entry, timestamp) }))
    .filter((segment) => segment.elements.length > 0);

  if (segments.length === 0 && runToolCalls.length === 0) {
    return undefined;
  }

  const elements: MessageElement[] = [];

  if (segments.length === 0) {
    // The run produced tool calls but no surviving assistant text; still render
    // the tool calls so the turn is not dropped.
    for (const toolCall of runToolCalls) {
      elements.push({
        type: "tool_call",
        toolCallId: toolCall.toolCallId,
        timestamp,
        runId,
        toolCall: conversationToolCallToLiveRecord(toolCall),
      });
    }
  } else {
    const toolsBySegment = assignToolCallsToSegments(segments, runToolCalls);
    segments.forEach((segment, segmentIndex) => {
      elements.push(...segment.elements);
      for (const toolCall of toolsBySegment[segmentIndex] ?? []) {
        elements.push({
          type: "tool_call",
          toolCallId: toolCall.toolCallId,
          timestamp,
          runId,
          toolCall: conversationToolCallToLiveRecord(toolCall),
        });
      }
    });
  }

  const id =
    segments[0]?.entry.messageId ??
    segments[0]?.entry.id ??
    runEntries[0]?.messageId ??
    runEntries[0]?.id ??
    `run-${runId}`;

  return {
    id,
    role: "assistant",
    timestamp,
    elements,
  };
}

function assistantElementsForEntry(
  entry: ConversationMessage,
  timestamp: number,
): MessageElement[] {
  const parts = contentPartsForEntry(entry);
  if (parts.length === 0) {
    const content = entry.contentText.trim();
    return content ? [{ type: "text", content, timestamp }] : [];
  }

  const elements: MessageElement[] = [];
  for (const part of parts) {
    const content = part.text.trim();
    if (!content) {
      continue;
    }
    elements.push({
      type: part.type,
      content,
      timestamp,
    });
  }

  return elements;
}

function displayTextForEntry(entry: ConversationMessage): string {
  const parts = contentPartsForEntry(entry).filter((part) => part.type === "text");
  if (parts.length === 0) {
    return entry.contentText.trim();
  }
  return parts
    .map((part) => part.text.trim())
    .filter(Boolean)
    .join("\n\n");
}

function contentPartsForEntry(
  entry: ConversationMessage,
): Array<{ type: "reasoning" | "text"; text: string }> {
  return entry.contentParts ?? entry.content?.parts ?? [];
}

/**
 * Group a run's tool calls under the text segment that triggered them. Tools are
 * linked via `parentMessageId`; any without a resolvable parent are distributed
 * across segments in event order as a fallback.
 */
function assignToolCallsToSegments(
  segments: AssistantSegment[],
  runToolCalls: ConversationToolCall[],
): ConversationToolCall[][] {
  const toolsBySegment: ConversationToolCall[][] = segments.map(() => []);
  if (segments.length === 0 || runToolCalls.length === 0) {
    return toolsBySegment;
  }

  const sortedTools = [...runToolCalls].sort(
    (left, right) => toolCallSortKey(left) - toolCallSortKey(right),
  );

  const segmentIndexById = new Map<string, number>();
  segments.forEach((segment, segmentIndex) => {
    segmentIndexById.set(segment.entry.messageId ?? segment.entry.id, segmentIndex);
  });

  const unassigned: ConversationToolCall[] = [];
  for (const toolCall of sortedTools) {
    const segmentIndex = toolCall.parentMessageId
      ? segmentIndexById.get(toolCall.parentMessageId)
      : undefined;
    if (segmentIndex !== undefined) {
      toolsBySegment[segmentIndex].push(toolCall);
    } else {
      unassigned.push(toolCall);
    }
  }

  distributeUnassignedToolCalls(toolsBySegment, unassigned);
  return toolsBySegment;
}

/**
 * Spread tool calls that could not be linked to a segment across the available
 * segments in order: one each when they fit, otherwise the overflow trails the
 * final segment (mirrors the web restore fallback).
 */
function distributeUnassignedToolCalls(
  toolsBySegment: ConversationToolCall[][],
  unassigned: ConversationToolCall[],
): void {
  const segmentCount = toolsBySegment.length;
  if (segmentCount === 0 || unassigned.length === 0) {
    return;
  }

  if (unassigned.length <= segmentCount) {
    unassigned.forEach((toolCall, toolIndex) => {
      toolsBySegment[toolIndex].push(toolCall);
    });
    return;
  }

  for (let toolIndex = 0; toolIndex < segmentCount - 1; toolIndex += 1) {
    toolsBySegment[toolIndex].push(unassigned[toolIndex]);
  }
  const lastSegmentIndex = segmentCount - 1;
  for (let toolIndex = segmentCount - 1; toolIndex < unassigned.length; toolIndex += 1) {
    toolsBySegment[lastSegmentIndex].push(unassigned[toolIndex]);
  }
}

/** Earliest user-message timestamp per run, used to stamp assistant turns. */
function userTimestampsByRun(
  sortedMessages: ConversationMessage[],
): Map<string, number> {
  const timestamps = new Map<string, number>();
  for (const entry of sortedMessages) {
    if (entry.role !== "user" || timestamps.has(entry.runId)) {
      continue;
    }
    timestamps.set(entry.runId, timestampFromIso(entry.createdAt, Date.now()));
  }
  return timestamps;
}

export function conversationToToolCalls(
  toolCalls: ConversationToolCall[],
): LiveToolCallRecord[] {
  return [...toolCalls]
    .sort((left, right) => toolCallSortKey(left) - toolCallSortKey(right))
    .map(conversationToolCallToLiveRecord);
}

function conversationToolCallToLiveRecord(toolCall: ConversationToolCall): LiveToolCallRecord {
  const now = Date.now();
  const status = toolCall.status === "completed"
    ? "success"
    : toolCall.status === "failed"
      ? "failed"
      : "pending";
  const result = serializeToolCallResult(toolCall);
  const record: LiveToolCallRecord = {
    id: toolCall.toolCallId,
    name: toolCall.toolName ?? toolCall.name ?? "tool",
    status,
    runId: toolCall.runId,
    startedAtMs: now,
  };
  if (status !== "pending") {
    record.finishedAtMs = now;
  }
  if (result !== undefined) {
    record.result = result;
  }
  if (toolCall.args !== undefined) {
    record.args = toolCall.args;
  }
  if (toolCall.resultPreview !== undefined) {
    record.resultPreview = toolCall.resultPreview;
  }
  return record;
}

function groupToolCallsByRun(
  toolCalls: ConversationToolCall[],
): Map<string, ConversationToolCall[]> {
  const groups = new Map<string, ConversationToolCall[]>();
  for (const toolCall of toolCalls) {
    const bucket = groups.get(toolCall.runId) ?? [];
    bucket.push(toolCall);
    groups.set(toolCall.runId, bucket);
  }
  for (const [runId, calls] of groups) {
    groups.set(runId, [...calls].sort((left, right) => toolCallSortKey(left) - toolCallSortKey(right)));
  }
  return groups;
}

function toolCallSortKey(toolCall: ConversationToolCall): number {
  return (
    toolCall.callEventSeq ??
    toolCall.resultEventSeq ??
    toolCall.endEventSeq ??
    0
  );
}

function serializeToolCallResult(toolCall: ConversationToolCall): string | undefined {
  if (typeof toolCall.result === "string") return toolCall.result;
  if (toolCall.resultPreview) return toolCall.resultPreview;
  if (toolCall.result !== undefined) {
    try {
      return JSON.stringify(toolCall.result);
    } catch {
      return undefined;
    }
  }
  return undefined;
}

function timestampFromIso(value: string | undefined, fallback: number): number {
  if (!value) return fallback;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}
