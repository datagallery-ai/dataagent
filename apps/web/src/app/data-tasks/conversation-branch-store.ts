"use client";

import { useSyncExternalStore } from "react";
import type { SessionConversationDto } from "../../lib/config-api/types";

type ConversationBranchSnapshot = {
  conversations: Record<string, SessionConversationDto | undefined>;
};

let snapshot: ConversationBranchSnapshot = { conversations: {} };
const listeners = new Set<() => void>();

function emitChange() {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot() {
  return snapshot;
}

export function setConversationBranchSnapshot(
  threadId: string,
  conversation: SessionConversationDto,
) {
  snapshot = {
    conversations: {
      ...snapshot.conversations,
      [threadId]: conversation,
    },
  };
  emitChange();
}

export function clearConversationBranchSnapshot(threadId: string) {
  if (!(threadId in snapshot.conversations)) {
    return;
  }
  const next = { ...snapshot.conversations };
  delete next[threadId];
  snapshot = { conversations: next };
  emitChange();
}

export function useConversationBranchSnapshot(
  threadId: string | undefined | null,
): SessionConversationDto | null {
  const store = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  if (!threadId) return null;
  return store.conversations[threadId] ?? null;
}
