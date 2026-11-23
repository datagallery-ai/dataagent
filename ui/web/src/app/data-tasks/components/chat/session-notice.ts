import { useSyncExternalStore } from "react";

const notices = new Map<string, string>();
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

export function setSessionNotice(threadId: string, notice: string | null): void {
  const current = notices.get(threadId) ?? null;
  if (current === notice) return;
  if (notice) notices.set(threadId, notice);
  else notices.delete(threadId);
  emit();
}

export function useSessionNotice(threadId: string | undefined): string | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => (threadId ? notices.get(threadId) ?? null : null),
    () => null,
  );
}
