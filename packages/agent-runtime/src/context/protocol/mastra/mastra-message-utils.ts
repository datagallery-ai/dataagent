import type { MastraDBMessage } from "@mastra/core/agent";

import { hashContextContent, type ContextRetention } from "../../inventory/context-item.js";

export type GroupedConversationMessage = {
  id: string;
  index: number;
  message: MastraDBMessage;
};

export type ConversationTurnGroup = {
  id: string;
  kind: "turn";
  order: number;
  isCurrent: boolean;
  mandatory: boolean;
  retention: ContextRetention;
  members: GroupedConversationMessage[];
};

export const isConversationTurnStart = (message: MastraDBMessage): boolean =>
  message.role === "user" && !isToolObservationMessage(message);

export const isToolObservationMessage = (message: MastraDBMessage): boolean =>
  hasMastraMessageContentParts(message) && message.content.parts.some((part) => isToolResultPart(part));

export const filterModelVisibleMastraMessages = (messages: MastraDBMessage[]): MastraDBMessage[] =>
  messages.filter(hasMastraMessageContentParts);

export const groupMessagesByTurn = (messages: MastraDBMessage[]): ConversationTurnGroup[] => {
  const members = createGroupedMessages(filterModelVisibleMastraMessages(messages));
  const grouped: Array<{ id: string; order: number; members: GroupedConversationMessage[] }> = [];
  let current: { id: string; order: number; members: GroupedConversationMessage[] } | undefined;

  for (const member of members) {
    if (!current || isConversationTurnStart(member.message)) {
      current = { id: `turn-${member.id}`, order: grouped.length, members: [] };
      grouped.push(current);
    }
    current.members.push(member);
  }

  return grouped.map((group, index) => {
    const isCurrent = index === grouped.length - 1;
    return {
      ...group,
      kind: "turn" as const,
      isCurrent,
      mandatory: isCurrent,
      retention: isCurrent ? "active" : "historical"
    };
  });
};

const createGroupedMessages = (messages: MastraDBMessage[]): GroupedConversationMessage[] => {
  const explicitIds = new Set<string>();
  const anonymousOccurrences = new Map<string, number>();

  return messages.map((message, index) => {
    const explicitId = typeof message.id === "string" && message.id.length > 0 ? message.id : undefined;
    if (explicitId) {
      if (explicitIds.has(explicitId)) {
        throw new Error(`CONTEXT_DUPLICATE_MESSAGE_ID:${explicitId}`);
      }
      explicitIds.add(explicitId);
      return { id: explicitId, index, message };
    }

    const fingerprint = hashContextContent({ role: message.role, content: message.content });
    const occurrence = anonymousOccurrences.get(fingerprint) ?? 0;
    anonymousOccurrences.set(fingerprint, occurrence + 1);
    return { id: `anonymous-${fingerprint.slice(0, 16)}-${occurrence}`, index, message };
  });
};

const isToolResultPart = (part: unknown): boolean => {
  if (!isRecord(part) || typeof part.type !== "string") {
    return false;
  }
  if (part.type === "tool-result") {
    return true;
  }
  if (part.type === "tool-invocation" && isRecord(part.toolInvocation)) {
    return isCompletedToolState(part.toolInvocation.state);
  }
  return part.type.startsWith("tool-") && ("output" in part || "result" in part);
};

const isCompletedToolState = (state: unknown): boolean =>
  state === "result" || state === "output-available" || state === "output-error" || state === "output-denied";

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;

const hasMastraMessageContentParts = (message: MastraDBMessage): message is MastraDBMessage & {
  content: { parts: unknown[] };
} => isRecord(message.content) && Array.isArray(message.content.parts);
