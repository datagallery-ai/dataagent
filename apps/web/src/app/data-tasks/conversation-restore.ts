import type { Message } from "@ag-ui/core";
import type {
  ConversationCheckpointDto,
  ConversationMessageDto,
  ConversationToolCallDto,
  RestorableCustomEventDto,
  SessionConversationDto,
} from "../../lib/config-api/types";
import { ConfigApiError } from "../../lib/config-api/types";
import {
  formatCollaborationResponseDisplay,
  type CollaborationToolName,
} from "./components/chat/collaboration-response-display";
import type { CollaborationResponseRecord } from "./components/chat/collaboration-responses";
import {
  canResumeCollaborationInterrupt,
  hasCollaborationResponse,
} from "./collaboration-recap";
import {
  accumulateSessionUsage,
  archiveCurrentRunSegment,
  createInitialLiveRun,
  createInitialSessionUsage,
  deriveRunUsage,
  reconcileLiveRunArtifacts,
  reduceLiveRunEvent,
  type LiveRun,
  type SessionUsageStats,
} from "./live-run-state";

/** Pending HITL interaction restored from server conversation metadata. */
export type RestoredPendingInteraction = {
  threadId: string;
  runId: string;
  toolCallId: string;
  toolName: "ask_user" | "submit_plan";
  interruptEvent: unknown;
};

/** Maps server pending interactions into client-side resume records. */
export function pendingInteractionsFromConversation(
  threadId: string,
  dto: SessionConversationDto,
): RestoredPendingInteraction[] {
  const records: RestoredPendingInteraction[] = [];
  for (const interaction of dto.pendingInteractions ?? []) {
    if (interaction.interruptEvent === undefined) {
      continue;
    }
    records.push({
      threadId,
      runId: interaction.runId,
      toolCallId: interaction.toolCallId,
      toolName: interaction.toolName,
      interruptEvent: interaction.interruptEvent,
    });
  }
  return records;
}

/** Replays persisted CUSTOM events into LiveRun after tool-call hydrate. */
export function replayRestorableCustomEvents(
  state: LiveRun,
  dto: SessionConversationDto,
): LiveRun {
  const events = [...(dto.restorableCustomEvents ?? [])].sort(
    (left, right) => (left.seq ?? 0) - (right.seq ?? 0),
  );
  let next = state;
  for (const entry of events) {
    if (
      next.runId &&
      RUN_SCOPED_RESTORABLE_CUSTOM_EVENTS.has(entry.name) &&
      entry.runId !== next.runId
    ) {
      continue;
    }
    next = reduceLiveRunEvent(next, {
      type: "CUSTOM",
      name: entry.name,
      value: entry.value,
    } as Parameters<typeof reduceLiveRunEvent>[1]);
  }
  return next;
}

export function hydrateSessionUsageFromConversation(
  dto: SessionConversationDto,
): SessionUsageStats {
  const events = sortRestorableCustomEvents(dto.restorableCustomEvents ?? []);
  let session = createInitialSessionUsage();

  const checkpoints = checkpointByRunId(dto);
  for (const segment of collectRestorableRunSegments(dto)) {
    const status = hydratedSegmentStatus(checkpoints.get(segment.runId));
    if (status === "suspended" || status === "running" || status === "idle" || status === "canceled") {
      continue;
    }

    let run = reduceLiveRunEvent(createInitialLiveRun(), {
      type: "RUN_STARTED",
      runId: segment.runId,
    });
    for (const entry of events) {
      if (entry.runId !== segment.runId || entry.name !== "token_usage") {
        continue;
      }
      run = reduceLiveRunEvent(run, {
        type: "CUSTOM",
        name: entry.name,
        value: entry.value,
      });
    }
    run =
      status === "failed"
        ? reduceLiveRunEvent(run, {
            type: "RUN_ERROR",
            message: failedRunErrorMessage(dto, segment.runId),
          })
        : reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
    session = accumulateSessionUsage(session, deriveRunUsage(run), status);
  }

  return session;
}

export function sortRestorableCustomEvents(
  events: RestorableCustomEventDto[],
): RestorableCustomEventDto[] {
  return [...events].sort((left, right) => (left.seq ?? 0) - (right.seq ?? 0));
}

/** Whether the client should fetch server conversation history for this thread. */
export function shouldRestoreConversation(input: {
  conversationMemoryEnabled: boolean;
  messageCount: number;
  isRunning: boolean;
  alreadyRestored: boolean;
}): boolean {
  return (
    input.conversationMemoryEnabled &&
    input.messageCount === 0 &&
    !input.isRunning &&
    !input.alreadyRestored
  );
}

/** Whether conversation restore should wait for active streaming/run state to settle. */
export function isConversationRestoreRunActive(input: {
  agentIsRunning: boolean;
  liveRunStatus?: LiveRun["runStatus"] | undefined;
}): boolean {
  return (
    input.agentIsRunning ||
    input.liveRunStatus === "running" ||
    input.liveRunStatus === "suspended"
  );
}

/** True when agent chat messages already reflect the persisted conversation. */
export function agentMessagesMatchConversation(
  agentMessages: unknown,
  dto: SessionConversationDto,
): boolean {
  const expected = conversationToAgentMessages(dto);
  if (expected.length === 0) {
    return true;
  }
  if (!Array.isArray(agentMessages) || agentMessages.length === 0) {
    return false;
  }
  if (agentMessages.length !== expected.length) {
    return false;
  }
  const lastAgent = agentMessages[agentMessages.length - 1] as { id?: string } | undefined;
  const lastExpected = expected[expected.length - 1];
  return lastAgent?.id === lastExpected?.id;
}

function messageText(message: unknown): string | undefined {
  if (typeof message !== "object" || message === null) {
    return undefined;
  }
  const content = (message as { content?: unknown }).content;
  if (typeof content === "string") {
    return content.trim() || undefined;
  }
  if (!Array.isArray(content)) {
    return undefined;
  }
  const text = content
    .map((part) => {
      if (typeof part === "string") {
        return part;
      }
      if (typeof part === "object" && part !== null) {
        const record = part as { type?: unknown; text?: unknown };
        if (record.type === "text" && typeof record.text === "string") {
          return record.text;
        }
      }
      return "";
    })
    .join("")
    .trim();
  return text || undefined;
}

function hasLocalMessagesAfterRestoredPrefix(
  agentMessages: unknown,
  expected: Message[],
): boolean {
  if (!Array.isArray(agentMessages) || agentMessages.length <= expected.length) {
    return false;
  }

  for (let index = 0; index < expected.length; index += 1) {
    const agent = agentMessages[index] as { id?: unknown } | undefined;
    if (agent?.id !== expected[index]?.id) {
      return false;
    }
  }

  const trailing = agentMessages.slice(expected.length);
  const expectedIds = new Set(expected.map((message) => message.id));
  return trailing.some((message) => {
    const item = message as { id?: unknown; role?: unknown } | undefined;
    if (!item || (item.role !== "user" && item.role !== "assistant")) {
      return false;
    }
    if (!messageText(item)) {
      return false;
    }
    return typeof item.id !== "string" || !expectedIds.has(item.id);
  });
}

/** Whether chat messages should be replaced from server conversation metadata. */
export function shouldRestoreConversationMessages(input: {
  conversationMemoryEnabled: boolean;
  isRunning: boolean;
  replaceExistingMessages: boolean;
  agentMessages: unknown;
  dto: SessionConversationDto;
}): boolean {
  if (!input.conversationMemoryEnabled || input.isRunning) {
    return false;
  }
  if (
    !input.replaceExistingMessages &&
    Array.isArray(input.agentMessages) &&
    input.agentMessages.length > 0
  ) {
    return false;
  }
  const expected = conversationToAgentMessages(input.dto);
  if (expected.length === 0) {
    return false;
  }
  if (hasLocalMessagesAfterRestoredPrefix(input.agentMessages, expected)) {
    return false;
  }
  return !agentMessagesMatchConversation(input.agentMessages, input.dto);
}

/** Whether live-run tool timeline should be rebuilt from persisted tool calls. */
export function shouldHydrateLiveRunFromConversation(
  state: LiveRun,
  dto: SessionConversationDto,
): boolean {
  const runSegments = collectRestorableRunSegments(dto);
  if (runSegments.length === 0) {
    return false;
  }
  // AG-UI may replay RUN_STARTED before REST hydrate finishes, leaving a running
  // shell with empty toolCalls and bogus runHistory boundaries.
  if (dto.toolCalls.length > 0 && state.toolCalls.length === 0) {
    return true;
  }
  if (state.toolCalls.length < dto.toolCalls.length) {
    return true;
  }
  const liveToolIds = new Set(state.toolCalls.map((call) => call.id));
  if (dto.toolCalls.some((toolCall) => !liveToolIds.has(toolCall.toolCallId))) {
    return true;
  }
  const latest = runSegments.at(-1);
  if (!latest) {
    return false;
  }
  const expectedStatus = hydratedSegmentStatus(checkpointByRunId(dto).get(latest.runId));
  return state.runId !== latest.runId || state.runStatus !== expectedStatus;
}

/** Latest user utterance in a persisted conversation (for console overview). */
export function isCollaborationEchoUserMessage(text: string): boolean {
  const normalized = text.trim().toLowerCase().replace(/\s+/g, " ");
  if (!normalized) return false;
  return (
    /^调用\s*ask\s*_?user(\s+tool)?[.!]?$/.test(normalized) ||
    /^调用\s*submit\s*_?plan(\s+tool)?[.!]?$/.test(normalized) ||
    /^call\s*ask\s*_?user(\s+tool)?[.!]?$/.test(normalized)
  );
}

export function normalizeUserQuestionText(text: string): string | undefined {
  const trimmed = text.trim().replace(/^(undefined\s*)+/i, "").trim();
  return trimmed || undefined;
}

export function latestUserQuestionFromConversation(
  dto: SessionConversationDto,
): string | undefined {
  const sorted = [...dto.messages].sort((left, right) => left.position - right.position);
  let fallback: string | undefined;
  for (let index = sorted.length - 1; index >= 0; index -= 1) {
    const entry = sorted[index];
    if (entry?.role !== "user") {
      continue;
    }
    const text = normalizeUserQuestionText(entry.contentText);
    if (!text) continue;
    fallback ??= text;
    if (!isCollaborationEchoUserMessage(text)) {
      return text;
    }
  }
  return fallback;
}

export function isIgnorableConversationRestoreError(error: unknown): boolean {
  if (error instanceof ConfigApiError) {
    return error.status === 404 || error.code === "RESOURCE_NOT_FOUND";
  }
  if (typeof error !== "object" || error === null) {
    return false;
  }
  const record = error as { status?: unknown; code?: unknown };
  return record.status === 404 || record.code === "RESOURCE_NOT_FOUND";
}

type RestoredToolCall = {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
};

const ORPHANED_RUN_ASSISTANT_PLACEHOLDER =
  "Previous request failed before the assistant produced a response.";

const COLLABORATION_TOOL_NAMES = new Set<CollaborationToolName>([
  "ask_user",
  "submit_plan",
]);

const RUN_SCOPED_RESTORABLE_CUSTOM_EVENTS = new Set([
  "context.compiled",
  "context.prompt-verified",
  "goal.updated",
  "run.config.resolved",
  "skill.selection",
  "token_usage",
  "token_usage.correlation",
  "workspace.metadata",
  "sandbox.output",
]);

type ChoiceOption = { label: string; value: string; description?: string };

function parseRecord(value: unknown): Record<string, unknown> | undefined {
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value) as unknown;
      return typeof parsed === "object" && parsed !== null
        ? (parsed as Record<string, unknown>)
        : undefined;
    } catch {
      return undefined;
    }
  }
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : undefined;
}

function serializeToolCallArguments(toolCall: ConversationToolCallDto): string {
  if (toolCall.args !== undefined) {
    if (typeof toolCall.args === "string") return toolCall.args;
    try {
      return JSON.stringify(toolCall.args);
    } catch {
      return "{}";
    }
  }
  return toolCall.resultPreview ?? "{}";
}

function normalizeChoiceOptions(raw: unknown): ChoiceOption[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .map((item): ChoiceOption | null => {
      if (typeof item === "string" && item.trim()) {
        const label = item.trim();
        return { label, value: label };
      }
      const record = parseRecord(item);
      if (!record) return null;
      const label =
        typeof record.label === "string"
          ? record.label
          : typeof record.value === "string"
            ? record.value
            : null;
      if (!label) return null;
      return {
        label,
        value: typeof record.value === "string" ? record.value : label,
        ...(typeof record.description === "string"
          ? { description: record.description }
          : {}),
      };
    })
    .filter((item): item is ChoiceOption => Boolean(item));
}

function readCollaborationArgs(toolCall: ConversationToolCallDto): {
  question?: string;
  plan?: string;
  options: ChoiceOption[];
} {
  const record = parseRecord(toolCall.args) ?? {};
  const suspendPayload = parseRecord(record.suspendPayload) ?? {};
  const options = normalizeChoiceOptions(record.options ?? suspendPayload.options);
  const question =
    typeof record.question === "string"
      ? record.question
      : typeof suspendPayload.question === "string"
        ? suspendPayload.question
        : typeof record.title === "string"
          ? record.title
          : undefined;
  const plan =
    typeof record.plan === "string"
      ? record.plan
      : typeof suspendPayload.plan === "string"
        ? suspendPayload.plan
        : undefined;
  return { question, plan, options };
}

function parseCollaborationResumeValue(
  toolName: CollaborationToolName,
  result: unknown,
): unknown {
  if (result === undefined) return undefined;
  if (toolName === "submit_plan") {
    const record = parseRecord(result);
    if (record && typeof record.action === "string") {
      return {
        action: record.action,
        ...(typeof record.feedback === "string" ? { feedback: record.feedback } : {}),
      };
    }
  }
  if (typeof result === "string") {
    const trimmed = result.trim();
    if (!trimmed) return undefined;
    const record = parseRecord(trimmed);
    if (record) {
      if (record.response !== undefined) return record.response;
      if (toolName === "submit_plan" && typeof record.action === "string") {
        return {
          action: record.action,
          ...(typeof record.feedback === "string" ? { feedback: record.feedback } : {}),
        };
      }
      if (typeof record.content === "string" && record.content.trim()) {
        const content = record.content.trim();
        const answered = content.match(/^User answered:\s*(.+)$/iu);
        if (answered?.[1]) {
          return answered[1].trim();
        }
        return content;
      }
    }
    return trimmed;
  }
  if (typeof result === "object" && result !== null) {
    const record = result as Record<string, unknown>;
    if (record.response !== undefined) return record.response;
    if (toolName === "submit_plan" && typeof record.action === "string") {
      return {
        action: record.action,
        ...(typeof record.feedback === "string" ? { feedback: record.feedback } : {}),
      };
    }
    if (typeof record.content === "string" && record.content.trim()) {
      const content = record.content.trim();
      const answered = content.match(/^User answered:\s*(.+)$/iu);
      if (answered?.[1]) {
        return answered[1].trim();
      }
      return content;
    }
  }
  return undefined;
}

function resolvePersistedMessageId(
  sortedEntries: ConversationMessageDto[],
  messageId: string,
): string | undefined {
  const entryIndexByMessageId = new Map<string, number>();
  sortedEntries.forEach((entry, index) => {
    entryIndexByMessageId.set(entry.messageId ?? entry.id, index);
  });
  const index = entryIndexByMessageId.get(messageId);
  if (index === undefined) {
    return undefined;
  }
  const entry = sortedEntries[index];
  return entry?.messageId ?? entry?.id;
}

/**
 * Legacy fallback for sessions whose TOOL_CALL_START.parentMessageId was never
 * persisted as a conversation row. New runs should persist `kind: "tool_parent"`
 * assistants on the backend; keep this path only for old data.
 */
export function syntheticToolParentMessageId(parentMessageId: string): string {
  return `restored-tool-parent:${parentMessageId}`;
}

function groupToolCallsByParentMessageId(
  toolCalls: ConversationToolCallDto[],
): ConversationToolCallDto[][] {
  const groups = new Map<string, ConversationToolCallDto[]>();
  const order: string[] = [];

  for (const toolCall of toolCalls) {
    const key = toolCall.parentMessageId ?? `__solo__:${toolCall.toolCallId}`;
    if (!groups.has(key)) {
      order.push(key);
      groups.set(key, []);
    }
    groups.get(key)?.push(toolCall);
  }

  return order
    .map((key) => groups.get(key) ?? [])
    .map((group) =>
      [...group].sort((left, right) => toolCallSortKey(left) - toolCallSortKey(right)),
    )
    .sort((left, right) => {
      const leftKey = left[0] ? toolCallSortKey(left[0]) : 0;
      const rightKey = right[0] ? toolCallSortKey(right[0]) : 0;
      return leftKey - rightKey;
    });
}

function insertSyntheticToolParentMessages(
  messages: Message[],
  sortedEntries: ConversationMessageDto[],
  toolCalls: ConversationToolCallDto[],
): Message[] {
  const orphanedParentIds = new Set<string>();
  for (const toolCall of toolCalls) {
    if (!toolCall.parentMessageId) {
      continue;
    }
    if (resolvePersistedMessageId(sortedEntries, toolCall.parentMessageId)) {
      continue;
    }
    orphanedParentIds.add(toolCall.parentMessageId);
  }
  if (orphanedParentIds.size === 0) {
    return messages;
  }

  const placeholders = [...orphanedParentIds]
    .map((parentMessageId) => ({
      parentMessageId,
      minSeq: Math.min(
        ...toolCalls
          .filter((toolCall) => toolCall.parentMessageId === parentMessageId)
          .map((toolCall) => toolCallSortKey(toolCall)),
      ),
      message: {
        id: syntheticToolParentMessageId(parentMessageId),
        role: "assistant" as const,
        content: "",
      },
    }))
    .sort((left, right) => left.minSeq - right.minSeq);

  let next = [...messages];
  for (const placeholder of placeholders) {
    const insertAt = orphanPlaceholderInsertIndex(
      next,
      sortedEntries,
      toolCalls,
      placeholder.minSeq,
    );
    next = [
      ...next.slice(0, insertAt),
      placeholder.message,
      ...next.slice(insertAt),
    ];
  }
  return next;
}

/**
 * Place an orphan tool-parent placeholder where its callEventSeq belongs relative
 * to persisted assistants that own tools. Avoids dumping every orphan before the
 * first assistant (which scrambled write_file → publish_artifact order).
 *
 * Legacy only: new runs persist empty tool-parent rows; this reorders synthetic
 * orphans for historical sessions that still lack those rows.
 */
function orphanPlaceholderInsertIndex(
  messages: Message[],
  sortedEntries: ConversationMessageDto[],
  toolCalls: ConversationToolCallDto[],
  orphanMinSeq: number,
): number {
  let insertAt = messages.length;
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index];
    if (!message || message.role !== "assistant") {
      continue;
    }
    // Skip already-inserted synthetic parents when placing later orphans.
    if (message.id.startsWith("restored-tool-parent:")) {
      const ownedSeq = minToolSeqForMessageId(message.id, sortedEntries, toolCalls);
      if (ownedSeq !== undefined && ownedSeq <= orphanMinSeq) {
        insertAt = index + 1;
      } else if (ownedSeq !== undefined && ownedSeq > orphanMinSeq) {
        return index;
      }
      continue;
    }
    const ownedSeq = minToolSeqForMessageId(message.id, sortedEntries, toolCalls);
    if (ownedSeq === undefined) {
      continue;
    }
    if (ownedSeq > orphanMinSeq) {
      return index;
    }
    insertAt = index + 1;
  }
  return insertAt;
}

function minToolSeqForMessageId(
  messageId: string,
  sortedEntries: ConversationMessageDto[],
  toolCalls: ConversationToolCallDto[],
): number | undefined {
  let minSeq: number | undefined;
  for (const toolCall of toolCalls) {
    const ownerId = findAssistantMessageIdForToolCall(sortedEntries, toolCall);
    if (ownerId !== messageId) {
      continue;
    }
    const seq = toolCallSortKey(toolCall);
    minSeq = minSeq === undefined ? seq : Math.min(minSeq, seq);
  }
  return minSeq;
}

function createRunFacts(dto: SessionConversationDto): {
  assistantRunIds: Set<string>;
  pendingRunIds: Set<string>;
  toolRunIds: Set<string>;
} {
  return {
    assistantRunIds: new Set(
      dto.messages
        .filter((message) => message.role === "assistant")
        .map((message) => message.runId),
    ),
    pendingRunIds: new Set(
      (dto.pendingInteractions ?? []).map((interaction) => interaction.runId),
    ),
    toolRunIds: new Set(dto.toolCalls.map((toolCall) => toolCall.runId)),
  };
}

function checkpointByRunId(
  dto: SessionConversationDto,
): Map<string, ConversationCheckpointDto> {
  const map = new Map<string, ConversationCheckpointDto>();
  for (const checkpoint of dto.checkpoints ?? []) {
    map.set(checkpoint.runId, checkpoint);
  }
  return map;
}

function timestampFromIso(value: string | undefined): number | undefined {
  if (!value) {
    return undefined;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function runStatusFromCheckpoint(
  checkpoint: ConversationCheckpointDto | undefined,
): LiveRun["runStatus"] | undefined {
  if (!checkpoint) {
    return undefined;
  }
  switch (checkpoint.status) {
    case "queued":
    case "running":
      return "running";
    case "suspended":
      return "suspended";
    case "canceled":
      return "canceled";
    case "failed":
      return "failed";
    case "completed":
      return "completed";
    default:
      return undefined;
  }
}

function failedRunErrorMessage(
  dto: SessionConversationDto,
  runId: string | undefined,
): string {
  if (!runId) {
    return ORPHANED_RUN_ASSISTANT_PLACEHOLDER;
  }
  const checkpoint = checkpointByRunId(dto).get(runId);
  return checkpoint?.errorMessage?.trim() || ORPHANED_RUN_ASSISTANT_PLACEHOLDER;
}

function shouldInsertOrphanedRunPlaceholder(
  _entry: ConversationMessageDto,
  _sortedEntries: ConversationMessageDto[],
  _index: number,
  _facts: ReturnType<typeof createRunFacts>,
): boolean {
  return false;
}

function orphanedRunAssistantPlaceholder(runId: string): Message {
  return {
    id: `orphan-run:${runId}:assistant-placeholder`,
    role: "assistant",
    content: ORPHANED_RUN_ASSISTANT_PLACEHOLDER,
  };
}

function findAssistantMessageIdForToolCall(
  sortedEntries: ConversationMessageDto[],
  toolCall: ConversationToolCallDto,
): string | undefined {
  if (toolCall.parentMessageId) {
    const resolved = resolvePersistedMessageId(sortedEntries, toolCall.parentMessageId);
    if (resolved) {
      return resolved;
    }
    return syntheticToolParentMessageId(toolCall.parentMessageId);
  }

  const entryIndexByMessageId = new Map<string, number>();
  sortedEntries.forEach((entry, index) => {
    entryIndexByMessageId.set(entry.messageId ?? entry.id, index);
  });

  if (toolCall.resultMessageId) {
    const resultIndex = entryIndexByMessageId.get(toolCall.resultMessageId);
    if (resultIndex !== undefined) {
      for (let index = resultIndex - 1; index >= 0; index -= 1) {
        const entry = sortedEntries[index];
        if (entry?.role === "assistant" && entry.runId === toolCall.runId) {
          return entry.messageId ?? entry.id;
        }
      }
    }
  }

  return undefined;
}

function assistantMessageIdsForRun(
  sortedEntries: ConversationMessageDto[],
  runId: string,
  restoredMessageIds: Set<string>,
): string[] {
  const ids: string[] = [];
  for (const entry of sortedEntries) {
    if (entry.role !== "assistant" || entry.runId !== runId) {
      continue;
    }
    const id = entry.messageId ?? entry.id;
    if (restoredMessageIds.has(id)) {
      ids.push(id);
    }
  }
  return ids;
}

function assistantMessageIdsToRestore(
  sortedEntries: ConversationMessageDto[],
  toolCalls: ConversationToolCallDto[],
): Set<string> {
  const ids = new Set<string>();
  for (const entry of sortedEntries) {
    if (entry.role !== "assistant") {
      continue;
    }
    const id = entry.messageId ?? entry.id;
    if (entry.contentText.trim()) {
      ids.add(id);
    }
  }

  for (const toolCall of toolCalls) {
    const ownerId = findAssistantMessageIdForToolCall(sortedEntries, toolCall);
    if (ownerId) {
      ids.add(ownerId);
    }
  }

  const toolsByRun = new Map<string, ConversationToolCallDto[]>();
  for (const toolCall of toolCalls) {
    const bucket = toolsByRun.get(toolCall.runId) ?? [];
    bucket.push(toolCall);
    toolsByRun.set(toolCall.runId, bucket);
  }
  for (const [runId, runTools] of toolsByRun) {
    const assistants = sortedEntries.filter(
      (entry) => entry.role === "assistant" && entry.runId === runId,
    );
    if (assistants.length > 0 && assistants.length <= runTools.length) {
      for (const entry of assistants) {
        ids.add(entry.messageId ?? entry.id);
      }
    }
  }

  return ids;
}

function assignToolCallsByAssistantOrder(
  sortedEntries: ConversationMessageDto[],
  messages: Message[],
  toolCalls: ConversationToolCallDto[],
  alreadyAssigned: Set<string>,
): Map<string, RestoredToolCall[]> {
  const restoredMessageIds = new Set(messages.map((message) => message.id));
  const toolsByRun = new Map<string, ConversationToolCallDto[]>();

  for (const toolCall of toolCalls) {
    if (alreadyAssigned.has(toolCall.toolCallId)) {
      continue;
    }
    const existing = toolsByRun.get(toolCall.runId) ?? [];
    existing.push(toolCall);
    toolsByRun.set(toolCall.runId, existing);
  }

  const toolsByMessageId = new Map<string, RestoredToolCall[]>();

  for (const [runId, runTools] of toolsByRun) {
    const sortedGroups = groupToolCallsByParentMessageId(runTools);
    const assistantIds = assistantMessageIdsForRun(
      sortedEntries,
      runId,
      restoredMessageIds,
    );
    if (assistantIds.length === 0 || sortedGroups.length === 0) {
      continue;
    }

    sortedGroups.forEach((group, index) => {
      const messageId = assistantIds[index] ?? assistantIds[assistantIds.length - 1];
      if (!messageId) return;
      const restoredCalls = group.map(toRestoredToolCall);
      const existing = toolsByMessageId.get(messageId) ?? [];
      toolsByMessageId.set(messageId, [...existing, ...restoredCalls]);
    });
  }

  return toolsByMessageId;
}

function resolveToolCallAssistantMessageId(
  sortedEntries: ConversationMessageDto[],
  toolCall: ConversationToolCallDto,
  toolCalls: ConversationToolCallDto[],
): string | undefined {
  const direct = findAssistantMessageIdForToolCall(sortedEntries, toolCall);
  if (direct) {
    return direct;
  }

  const restoredMessageIds = new Set<string>();
  for (const entry of sortedEntries) {
    if (entry.role !== "assistant" || entry.runId !== toolCall.runId) {
      continue;
    }
    if (!entry.contentText.trim()) {
      continue;
    }
    restoredMessageIds.add(entry.messageId ?? entry.id);
  }

  const assistants = assistantMessageIdsForRun(
    sortedEntries,
    toolCall.runId,
    restoredMessageIds,
  );
  const sortedTools = toolCalls
    .filter((entry) => entry.runId === toolCall.runId)
    .sort((left, right) => toolCallSortKey(left) - toolCallSortKey(right));
  const toolIndex = sortedTools.findIndex(
    (entry) => entry.toolCallId === toolCall.toolCallId,
  );
  if (toolIndex < 0 || assistants.length === 0) {
    return undefined;
  }
  if (toolIndex < assistants.length) {
    return assistants[toolIndex];
  }
  return assistants[assistants.length - 1];
}

function toRestoredToolCall(toolCall: ConversationToolCallDto): RestoredToolCall {
  const toolName = authoritativeToolName(toolCall);
  return {
    id: toolCall.toolCallId,
    type: "function",
    function: {
      name: toolName,
      arguments: serializeToolCallArguments(toolCall),
    },
  };
}

function attachRestoredToolCalls(
  messages: Message[],
  sortedEntries: ConversationMessageDto[],
  toolCalls: ConversationToolCallDto[],
): Message[] {
  if (toolCalls.length === 0) {
    return messages;
  }

  const toolsByMessageIndex = new Map<number, RestoredToolCall[]>();
  const assignedToolCallIds = new Set<string>();

  for (const toolCall of toolCalls) {
    const toolName = toolCall.toolName ?? toolCall.name;
    if (!toolName) {
      continue;
    }

    const assistantMessageId = findAssistantMessageIdForToolCall(sortedEntries, toolCall);
    if (!assistantMessageId) {
      continue;
    }

    const messageIndex = messages.findIndex((message) => message.id === assistantMessageId);
    if (messageIndex < 0) {
      continue;
    }

    assignedToolCallIds.add(toolCall.toolCallId);
    const existing = toolsByMessageIndex.get(messageIndex) ?? [];
    existing.push(toRestoredToolCall(toolCall));
    toolsByMessageIndex.set(messageIndex, existing);
  }

  const orderedAssignments = assignToolCallsByAssistantOrder(
    sortedEntries,
    messages,
    toolCalls,
    assignedToolCallIds,
  );

  for (const [messageId, toolCallEntries] of orderedAssignments) {
    const messageIndex = messages.findIndex((message) => message.id === messageId);
    if (messageIndex < 0) {
      continue;
    }
    const existing = toolsByMessageIndex.get(messageIndex) ?? [];
    toolsByMessageIndex.set(messageIndex, [...existing, ...toolCallEntries]);
  }

  return messages.map((message, index) => {
    const toolCallEntries = toolsByMessageIndex.get(index);
    if (!toolCallEntries?.length) {
      return message;
    }
    return {
      ...message,
      toolCalls: toolCallEntries,
    };
  });
}

function dedupeRestoredToolCalls(toolCalls: RestoredToolCall[]): RestoredToolCall[] {
  const seen = new Set<string>();
  const deduped: RestoredToolCall[] = [];
  for (const toolCall of toolCalls) {
    if (seen.has(toolCall.id)) {
      continue;
    }
    seen.add(toolCall.id);
    deduped.push(toolCall);
  }
  return deduped;
}

/**
 * Maps server-authoritative conversation messages to AG-UI chat messages.
 * Tool calls from `dto.toolCalls` are attached to the nearest preceding
 * assistant message in the same run so step UI can render historical tools.
 */
export function conversationToAgentMessages(
  dto: SessionConversationDto,
): Message[] {
  const sorted = [...dto.messages].sort((a, b) => a.position - b.position);
  const assistantIdsToRestore = assistantMessageIdsToRestore(sorted, dto.toolCalls);
  const runFacts = createRunFacts(dto);
  const messages: Message[] = [];

  for (const [index, entry] of sorted.entries()) {
    if (entry.role !== "user" && entry.role !== "assistant") {
      continue;
    }
    const content = entry.contentText.trim();
    const id = entry.messageId ?? entry.id;
    if (entry.role === "user") {
      if (!content) {
        continue;
      }
      messages.push({
        id,
        role: "user",
        content,
      });
      if (shouldInsertOrphanedRunPlaceholder(entry, sorted, index, runFacts)) {
        messages.push(orphanedRunAssistantPlaceholder(entry.runId));
      }
      continue;
    }
    if (!content && !assistantIdsToRestore.has(id)) {
      continue;
    }
    const contentParts = entry.contentParts?.filter(
      (part) =>
        (part.type === "reasoning" || part.type === "text") &&
        typeof part.text === "string" &&
        part.text.trim().length > 0,
    );
    messages.push({
      id,
      role: "assistant",
      content:
        contentParts && contentParts.length > 0
          ? contentParts.map((part) => ({ type: part.type, text: part.text }))
          : content,
    } as Message);
  }

  const withPlaceholders = insertSyntheticToolParentMessages(messages, sorted, dto.toolCalls);
  const restored = attachRestoredToolCalls(withPlaceholders, sorted, dto.toolCalls);
  return restored.map((message) => {
    if (!("toolCalls" in message) || !message.toolCalls?.length) {
      return message;
    }
    return {
      ...message,
      toolCalls: dedupeRestoredToolCalls(message.toolCalls),
    };
  });
}

/**
 * Rebuilds client-side collaboration response records from persisted tool-call
 * metadata so ask_user / submit_plan steps stay answered after session switch.
 */
export function collaborationResponsesFromConversation(
  threadId: string,
  dto: SessionConversationDto,
): Array<Omit<CollaborationResponseRecord, "id" | "createdAt">> {
  if (dto.toolCalls.length === 0) {
    return [];
  }

  const sortedEntries = [...dto.messages].sort((left, right) => left.position - right.position);
  const records: Array<Omit<CollaborationResponseRecord, "id" | "createdAt">> = [];

  for (const toolCall of dto.toolCalls) {
    const toolName = authoritativeCollaborationToolName(toolCall);
    if (!toolName) {
      continue;
    }
    if (toolCall.status !== "completed") {
      continue;
    }

    const resumeValue = parseCollaborationResumeValue(
      toolName,
      toolCall.result ?? toolCall.resultPreview,
    );
    if (resumeValue === undefined) {
      continue;
    }

    const { question, plan, options } = readCollaborationArgs(toolCall);
    records.push({
      threadId,
      toolCallId: toolCall.toolCallId,
      toolName,
      ...(question ? { question } : {}),
      ...(plan ? { plan } : {}),
      displayText: formatCollaborationResponseDisplay(toolName, resumeValue, options),
      assistantMessageId: resolveToolCallAssistantMessageId(
        sortedEntries,
        toolCall,
        dto.toolCalls,
      ),
    });
  }

  return records;
}

function toolCallSortKey(toolCall: ConversationToolCallDto): number {
  return (
    toolCall.callEventSeq ??
    toolCall.resultEventSeq ??
    toolCall.endEventSeq ??
    0
  );
}

function serializeToolCallResult(toolCall: ConversationToolCallDto): string | undefined {
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

type RestorableRunSegment = {
  runId: string;
  order: number;
  hasUser: boolean;
  hasAssistant: boolean;
  tools: ConversationToolCallDto[];
};

function collectRestorableRunSegments(dto: SessionConversationDto): RestorableRunSegment[] {
  const byRunId = new Map<string, RestorableRunSegment>();
  const sortedMessages = [...dto.messages].sort((left, right) => left.position - right.position);

  for (const message of sortedMessages) {
    if (!message.runId) {
      continue;
    }
    const existing = byRunId.get(message.runId) ?? {
      runId: message.runId,
      order: message.position,
      hasUser: false,
      hasAssistant: false,
      tools: [],
    };
    existing.order = Math.min(existing.order, message.position);
    if (message.role === "user" && message.contentText.trim()) {
      existing.hasUser = true;
    }
    if (message.role === "assistant") {
      existing.hasAssistant = true;
    }
    byRunId.set(message.runId, existing);
  }

  for (const toolCall of dto.toolCalls) {
    const order = toolCallSortKey(toolCall);
    const existing = byRunId.get(toolCall.runId) ?? {
      runId: toolCall.runId,
      order,
      hasUser: false,
      hasAssistant: false,
      tools: [],
    };
    if (!existing.hasUser && !existing.hasAssistant) {
      existing.order = Math.min(existing.order, order);
    }
    existing.tools.push(toolCall);
    byRunId.set(toolCall.runId, existing);
  }

  const checkpointRunIds = new Set((dto.checkpoints ?? []).map((checkpoint) => checkpoint.runId));
  for (const checkpoint of dto.checkpoints ?? []) {
    const order =
      checkpoint.messageStartPosition ??
      checkpoint.firstEventSeq ??
      byRunId.get(checkpoint.runId)?.order ??
      Number.MAX_SAFE_INTEGER;
    const existing = byRunId.get(checkpoint.runId);
    if (!existing) {
      byRunId.set(checkpoint.runId, {
        runId: checkpoint.runId,
        order,
        hasUser: false,
        hasAssistant: false,
        tools: [],
      });
      continue;
    }
    existing.order = Math.min(existing.order, order);
  }

  for (const ref of dto.runEventRefs) {
    const order =
      ref.firstSeq ??
      byRunId.get(ref.runId)?.order ??
      Number.MAX_SAFE_INTEGER;
    const existing = byRunId.get(ref.runId);
    if (!existing) {
      byRunId.set(ref.runId, {
        runId: ref.runId,
        order,
        hasUser: false,
        hasAssistant: false,
        tools: [],
      });
      continue;
    }
    existing.order = Math.min(existing.order, order);
  }

  return [...byRunId.values()]
    .filter(
      (segment) =>
        segment.hasUser ||
        segment.hasAssistant ||
        segment.tools.length > 0 ||
        checkpointRunIds.has(segment.runId),
    )
    .sort((left, right) => left.order - right.order)
    .map((segment) => ({
      ...segment,
      tools: [...segment.tools].sort(
        (left, right) => toolCallSortKey(left) - toolCallSortKey(right),
      ),
    }));
}

/**
 * Run status is now taken authoritatively from the backend checkpoint contract
 * (config-api `runCheckpointDto`). Historical runs that predate the checkpoint
 * contract fall back to "completed"; the frontend no longer guesses status from
 * tool presence or the user/assistant message shape.
 */
function hydratedSegmentStatus(
  checkpoint?: ConversationCheckpointDto,
): LiveRun["runStatus"] {
  return runStatusFromCheckpoint(checkpoint) ?? "completed";
}

/**
 * Authoritative tool name from the persisted tool-call contract. The backend emits
 * the canonical Mastra tool name on TOOL_CALL_START/END/RESULT, so the frontend no
 * longer infers ask_user vs submit_plan from args/result text.
 */
function authoritativeToolName(toolCall: ConversationToolCallDto): string {
  return toolCall.toolName ?? toolCall.name ?? "tool";
}

function authoritativeCollaborationToolName(
  toolCall: ConversationToolCallDto,
): CollaborationToolName | undefined {
  const name = toolCall.toolName ?? toolCall.name;
  return name && COLLABORATION_TOOL_NAMES.has(name as CollaborationToolName)
    ? (name as CollaborationToolName)
    : undefined;
}

function isPendingCollaborationRunEnd(tools: ConversationToolCallDto[]): boolean {
  // R-018: authoritative "awaiting user" flag from the backend — if any tool call is
  // flagged awaitingInteraction the run is suspended, no inference needed.
  if (tools.some((tool) => tool.awaitingInteraction === true)) {
    return true;
  }
  const last = tools.at(-1);
  if (!last) return false;
  const toolName = authoritativeCollaborationToolName(last);
  if (last.status === "pending") return true;
  if (!toolName) return false;
  return serializeToolCallResult(last) === undefined;
}

function applyConversationToolCall(state: LiveRun, toolCall: ConversationToolCallDto): LiveRun {
  const toolName = authoritativeToolName(toolCall);
  const toolCallId = toolCall.toolCallId;
  let next = reduceLiveRunEvent(state, {
    type: "TOOL_CALL_START",
    toolCallId,
    toolCallName: toolName,
    ...(toolCall.args && typeof toolCall.args === "object"
      ? { args: toolCall.args as Record<string, unknown> }
      : {}),
  });

  const result = serializeToolCallResult(toolCall);
  if (result !== undefined) {
    next = reduceLiveRunEvent(next, {
      type: "TOOL_CALL_RESULT",
      toolCallId,
      toolCallName: toolName,
      result,
    });
  } else if (toolCall.status === "failed") {
    next = reduceLiveRunEvent(next, {
      type: "TOOL_CALL_RESULT",
      toolCallId,
      toolCallName: toolName,
      result: JSON.stringify({ error: "Tool execution failed" }),
    });
  }

  return next;
}

function finalizeHydratedRunSegment(
  state: LiveRun,
  tools: ConversationToolCallDto[],
  checkpoint: ConversationCheckpointDto | undefined,
): LiveRun {
  const checkpointStatus = runStatusFromCheckpoint(checkpoint);
  if (checkpointStatus === "running") {
    return state;
  }
  if (checkpointStatus === "failed") {
    return reduceLiveRunEvent(state, {
      type: "RUN_ERROR",
      message: checkpoint?.errorMessage?.trim() || ORPHANED_RUN_ASSISTANT_PLACEHOLDER,
    });
  }
  if (checkpointStatus === "canceled") {
    return reduceLiveRunEvent(state, { type: "RUN_FINISHED", status: "canceled" });
  }
  if (tools.length === 0) {
    return state;
  }
  if (checkpointStatus === "suspended" || isPendingCollaborationRunEnd(tools)) {
    let next = reduceLiveRunEvent(state, {
      type: "STATE_DELTA",
      delta: [{ op: "replace", path: "/runStatus", value: "suspended" }],
    });
    next = reduceLiveRunEvent(next, { type: "RUN_FINISHED" });
    return next;
  }
  return reduceLiveRunEvent(state, { type: "RUN_FINISHED" });
}

function finalizeMessageOnlyHydratedRunSegment(
  state: LiveRun,
  segment: RestorableRunSegment,
  checkpoint: ConversationCheckpointDto | undefined,
  dto: SessionConversationDto,
): LiveRun {
  const checkpointStatus = runStatusFromCheckpoint(checkpoint);
  if (checkpointStatus === "running") {
    return state;
  }
  if (checkpointStatus === "suspended") {
    return reduceLiveRunEvent(state, {
      type: "STATE_DELTA",
      delta: [{ op: "replace", path: "/runStatus", value: "suspended" }],
    });
  }
  if (checkpointStatus === "canceled") {
    return reduceLiveRunEvent(state, { type: "RUN_FINISHED", status: "canceled" });
  }
  if (checkpointStatus === "failed") {
    return reduceLiveRunEvent(state, {
      type: "RUN_ERROR",
      message: checkpoint?.errorMessage?.trim() || ORPHANED_RUN_ASSISTANT_PLACEHOLDER,
    });
  }
  if (checkpointStatus === "completed") {
    return reduceLiveRunEvent(state, { type: "RUN_FINISHED" });
  }
  // Legacy fallback: no checkpoint for a user-only segment. Prefer backend
  // checkpoints (runs row / event-only / pending-interaction suspended). Remove
  // once all terminal runs emit checkpoints.
  if (segment.hasUser && !segment.hasAssistant) {
    return reduceLiveRunEvent(state, {
      type: "RUN_ERROR",
      message: failedRunErrorMessage(dto, segment.runId),
    });
  }
  return reduceLiveRunEvent(state, { type: "RUN_FINISHED" });
}

function applyHydratedRunCheckpointTiming(
  state: LiveRun,
  checkpoint: ConversationCheckpointDto | undefined,
): LiveRun {
  const startedAt = timestampFromIso(checkpoint?.startedAt);
  const finishedAt = timestampFromIso(checkpoint?.finishedAt);
  if (startedAt === undefined && finishedAt === undefined) {
    return state;
  }
  return {
    ...state,
    ...(startedAt !== undefined ? { runStartedAt: startedAt } : {}),
    ...(finishedAt !== undefined ? { runFinishedAt: finishedAt } : {}),
  };
}

function startNextHydratedRunGroup(state: LiveRun, runId?: string): LiveRun {
  if (state.runStatus === "suspended") {
    const archived: LiveRun = {
      ...state,
      runHistory: archiveCurrentRunSegment(state),
      runStartedAt: undefined,
      runFinishedAt: undefined,
      runStatus: "idle",
    };
    return reduceLiveRunEvent(archived, { type: "RUN_STARTED", ...(runId ? { runId } : {}) });
  }
  return reduceLiveRunEvent(state, { type: "RUN_STARTED", ...(runId ? { runId } : {}) });
}

function dtoWithRestorableEventsForRun(
  dto: SessionConversationDto,
  runId: string,
): SessionConversationDto {
  return {
    ...dto,
    restorableCustomEvents: (dto.restorableCustomEvents ?? []).filter(
      (event) => event.runId === runId,
    ),
  };
}

function mergePreservedLiveRunSessionData(previous: LiveRun, hydrated: LiveRun): LiveRun {
  const artifactIds = new Set(hydrated.artifacts.map((artifact) => artifact.id));
  const eventIds = new Set(hydrated.events.map((event) => event.id));
  const auditIds = new Set(hydrated.audits.map((audit) => audit.id));

  return {
    ...hydrated,
    artifacts: [
      ...hydrated.artifacts,
      ...previous.artifacts.filter((artifact) => !artifactIds.has(artifact.id)),
    ],
    events: [
      ...hydrated.events,
      ...previous.events.filter((event) => !eventIds.has(event.id)),
    ],
    audits: [
      ...hydrated.audits,
      ...previous.audits.filter((audit) => !auditIds.has(audit.id)),
    ],
  };
}

/**
 * Rebuilds console/trace tool-call state from persisted conversation metadata.
 * Used when chat messages are restored via REST instead of AG-UI event replay.
 */
export function hydrateLiveRunFromConversation(
  state: LiveRun,
  dto: SessionConversationDto,
): LiveRun {
  if (!shouldHydrateLiveRunFromConversation(state, dto)) {
    return state;
  }

  const runSegments = collectRestorableRunSegments(dto);
  const checkpoints = checkpointByRunId(dto);

  let next = createInitialLiveRun();
  for (const [index, segment] of runSegments.entries()) {
    const checkpoint = checkpoints.get(segment.runId);
    next =
      index === 0
        ? reduceLiveRunEvent(next, { type: "RUN_STARTED", runId: segment.runId })
        : startNextHydratedRunGroup(next, segment.runId);

    for (const toolCall of segment.tools) {
      next = applyConversationToolCall(next, toolCall);
    }

    next = replayRestorableCustomEvents(next, dtoWithRestorableEventsForRun(dto, segment.runId));

    next =
      segment.tools.length > 0
        ? finalizeHydratedRunSegment(next, segment.tools, checkpoint)
        : finalizeMessageOnlyHydratedRunSegment(next, segment, checkpoint, dto);
    next = applyHydratedRunCheckpointTiming(next, checkpoint);
  }

  // Workspace/sandbox signals come only from persisted workspace.metadata / sandbox.output
  // CUSTOM events (replayed above). The frontend no longer synthesizes them from tool-result
  // text — the backend is the authoritative source for these signals.
  return reconcileLiveRunArtifacts(mergePreservedLiveRunSessionData(state, next));
}

/**
 * Legacy defense-in-depth: when pendingInteractions exist but toolCalls were not
 * persisted (pre-HITL-atomic suspend), bootstrap a minimal suspended LiveRun.
 * New suspends persist TOOL_CALL_START + synthesize missing toolCalls on read;
 * keep this for old sessions only.
 */
export function hydratePendingInteractionLiveRun(
  state: LiveRun,
  threadId: string,
  dto: SessionConversationDto,
  collaborationResponses: CollaborationResponseRecord[],
): LiveRun {
  const pendingRecords = pendingInteractionsFromConversation(threadId, dto);
  if (pendingRecords.length === 0) {
    return state;
  }

  const unanswered = pendingRecords.filter(
    (record) => !hasCollaborationResponse(record.toolCallId, collaborationResponses),
  );
  if (unanswered.length === 0) {
    return state;
  }

  const target = unanswered[unanswered.length - 1];
  if (
    target &&
    canResumeCollaborationInterrupt({
      toolCallId: target.toolCallId,
      collaborationResponses,
      liveRun: state,
      liveRunStatus: state.runStatus,
    })
  ) {
    return state;
  }

  let next =
    state.runStatus === "idle" || state.runStatus === "completed"
      ? createInitialLiveRun()
      : state;

  if (next.runStatus === "running") {
    return next;
  }

  const runId = target?.runId;
  if (next.runStatus !== "suspended") {
    next = reduceLiveRunEvent(next, { type: "RUN_STARTED", ...(runId ? { runId } : {}) });
  }

  for (const record of unanswered) {
    if (next.toolCalls.some((call) => call.id === record.toolCallId)) {
      continue;
    }
    next = reduceLiveRunEvent(next, {
      type: "TOOL_CALL_START",
      toolCallId: record.toolCallId,
      toolCallName: record.toolName,
    });
  }

  if (next.runStatus !== "suspended") {
    next = reduceLiveRunEvent(next, {
      type: "STATE_DELTA",
      delta: [{ op: "replace", path: "/runStatus", value: "suspended" }],
    });
    next = reduceLiveRunEvent(next, { type: "RUN_FINISHED" });
  }

  return next;
}
