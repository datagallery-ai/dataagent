"use client";

import { useCallback, useSyncExternalStore } from "react";
import type { RestoredPendingInteraction } from "../../conversation-restore";

type RestoredInterruptStore = {
  byThread: Map<string, RestoredPendingInteraction[]>;
};

const storeState: RestoredInterruptStore = {
  byThread: new Map(),
};

// Stable empty reference: returning a fresh `[]` from getSnapshot on every
// render makes useSyncExternalStore think the store changed each time, causing
// an infinite render loop ("getSnapshot should be cached").
const EMPTY_INTERRUPTS: RestoredPendingInteraction[] = [];

const listeners = new Set<() => void>();

function emitChange(): void {
  listeners.forEach((listener) => listener());
}

export function hydrateRestoredInterrupts(
  records: RestoredPendingInteraction[],
): void {
  if (records.length === 0) {
    return;
  }
  const threadId = records[0]?.threadId;
  if (!threadId) {
    return;
  }
  storeState.byThread.set(threadId, records);
  emitChange();
}

export function clearRestoredInterrupts(threadId: string): void {
  if (!storeState.byThread.has(threadId)) {
    return;
  }
  storeState.byThread.delete(threadId);
  emitChange();
}

export function removeRestoredInterrupt(threadId: string, toolCallId: string): void {
  const existing = storeState.byThread.get(threadId) ?? [];
  const next = existing.filter((record) => record.toolCallId !== toolCallId);
  if (next.length === 0) {
    storeState.byThread.delete(threadId);
  } else {
    storeState.byThread.set(threadId, next);
  }
  emitChange();
}

export function getRestoredInterrupts(threadId: string): RestoredPendingInteraction[] {
  return storeState.byThread.get(threadId) ?? EMPTY_INTERRUPTS;
}

export function useRestoredInterrupts(threadId?: string): RestoredPendingInteraction[] {
  const subscribe = useCallback((listener: () => void) => {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);

  const getSnapshot = useCallback(
    () => (threadId ? getRestoredInterrupts(threadId) : EMPTY_INTERRUPTS),
    [threadId],
  );

  return useSyncExternalStore(subscribe, getSnapshot, () => EMPTY_INTERRUPTS);
}
