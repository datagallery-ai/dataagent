"use client";

import { useAgent } from "@copilotkit/react-core/v2";
import { useEffect, useSyncExternalStore } from "react";
import { messageTextContent } from "./assistant-thought-content";

type MessageLike = {
  id?: string;
  role?: string;
  content?: unknown;
  toolCalls?: unknown[];
};

function buildMessageSyncFingerprint(messages: MessageLike[]): string {
  return messages
    .map((message) => {
      const contentLength = messageTextContent(message.content).length;
      const reasoningLength = reasoningContentLength(message.content);
      const toolCallCount = Array.isArray(message.toolCalls) ? message.toolCalls.length : 0;
      const toolArgsLength = Array.isArray(message.toolCalls)
        ? message.toolCalls.reduce<number>((sum, call) => {
            const args =
              call &&
              typeof call === "object" &&
              "function" in call &&
              call.function &&
              typeof call.function === "object" &&
              "arguments" in call.function &&
              typeof call.function.arguments === "string"
                ? call.function.arguments.length
                : 0;
            return sum + args;
          }, 0)
        : 0;
      return `${message.id ?? "?"}:${message.role ?? "?"}:${contentLength}:${reasoningLength}:${toolCallCount}:${toolArgsLength}`;
    })
    .join("|");
}

function reasoningContentLength(content: unknown): number {
  if (!Array.isArray(content)) return 0;
  return content.reduce((sum, part) => {
    if (!part || typeof part !== "object" || !("type" in part)) return sum;
    const typed = part as { type?: unknown; text?: unknown };
    if (typed.type !== "reasoning" || typeof typed.text !== "string") return sum;
    return sum + typed.text.trim().length;
  }, 0);
}

type SyncSnapshot = {
  generation: number;
  messageCount: number;
  lastMessageId?: string;
  messageFingerprint: string;
  isRunning: boolean;
  runStatus: string;
};

let snapshot: SyncSnapshot = {
  generation: 0,
  messageCount: 0,
  messageFingerprint: "",
  isRunning: false,
  runStatus: "idle",
};

const listeners = new Set<() => void>();

function emitChange() {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getGenerationSnapshot() {
  return snapshot.generation;
}

function getFullSnapshot(): SyncSnapshot {
  return snapshot;
}

/** Subscribe to agent message / run status changes outside CopilotChat subtree. */
export function useAgentMessageRenderSnapshot(): SyncSnapshot {
  return useSyncExternalStore(subscribe, getFullSnapshot, getFullSnapshot);
}

/** Bump when agent messages / run status change so memoized assistant rows re-render. */
export function bumpAgentMessageRenderSync(input: {
  messageCount: number;
  lastMessageId?: string;
  messageFingerprint: string;
  isRunning: boolean;
  runStatus: string;
}) {
  if (
    snapshot.messageCount === input.messageCount &&
    snapshot.lastMessageId === input.lastMessageId &&
    snapshot.messageFingerprint === input.messageFingerprint &&
    snapshot.isRunning === input.isRunning &&
    snapshot.runStatus === input.runStatus
  ) {
    return;
  }
  snapshot = {
    generation: snapshot.generation + 1,
    messageCount: input.messageCount,
    lastMessageId: input.lastMessageId,
    messageFingerprint: input.messageFingerprint,
    isRunning: input.isRunning,
    runStatus: input.runStatus,
  };
  emitChange();
}

/** Subscribe inside StepAssistantMessage to bypass CopilotKit memo stale props. */
export function useAgentMessageRenderGeneration(): number {
  return useSyncExternalStore(subscribe, getGenerationSnapshot, getGenerationSnapshot);
}

/** Keeps assistant step cards in sync when CopilotKit memo skips prop updates. */
export function AgentMessageRenderSync({
  agentId,
  runStatus,
}: {
  agentId: string;
  runStatus: string;
}) {
  const { agent } = useAgent({ agentId });
  const messages = agent.messages ?? [];
  const messageFingerprint = buildMessageSyncFingerprint(messages);

  useEffect(() => {
    bumpAgentMessageRenderSync({
      messageCount: messages.length,
      lastMessageId: messages[messages.length - 1]?.id,
      messageFingerprint,
      isRunning: Boolean(agent.isRunning),
      runStatus,
    });
  }, [agent.isRunning, messageFingerprint, messages.length, runStatus]);

  return null;
}
