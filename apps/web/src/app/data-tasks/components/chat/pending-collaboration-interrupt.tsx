"use client";

import { useCallback, useSyncExternalStore, type ReactElement } from "react";

export type PendingCollaborationInterruptSource = "live" | "restored";

export type PendingCollaborationInterrupt = {
  threadId: string;
  toolCallId: string;
  element: ReactElement;
  source: PendingCollaborationInterruptSource;
};

const store = new Map<string, PendingCollaborationInterrupt>();
const listeners = new Set<() => void>();

function emitChange(): void {
  listeners.forEach((listener) => listener());
}

export function setPendingCollaborationInterrupt(
  entry: PendingCollaborationInterrupt,
): void {
  store.set(entry.threadId, entry);
  emitChange();
}

export function clearPendingCollaborationInterrupt(
  threadId: string,
  source?: PendingCollaborationInterruptSource,
): void {
  const existing = store.get(threadId);
  if (!existing) {
    return;
  }
  if (source && existing.source !== source) {
    return;
  }
  store.delete(threadId);
  emitChange();
}

export function clearAllPendingCollaborationInterrupts(): void {
  if (store.size === 0) {
    return;
  }
  store.clear();
  emitChange();
}

export function getPendingCollaborationInterrupt(
  threadId: string,
): PendingCollaborationInterrupt | undefined {
  return store.get(threadId);
}

export function usePendingCollaborationInterrupt(
  threadId: string | undefined,
): PendingCollaborationInterrupt | undefined {
  const subscribe = useCallback((listener: () => void) => {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);

  const getSnapshot = useCallback(
    () => (threadId ? store.get(threadId) : undefined),
    [threadId],
  );

  return useSyncExternalStore(subscribe, getSnapshot, () => undefined);
}
