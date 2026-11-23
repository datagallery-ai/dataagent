import { useSyncExternalStore } from "react";

export type ConversationScrollIntent =
  | { kind: "bottom" }
  | { kind: "preserve"; scrollTop: number };

type ConversationScrollState = {
  intent: ConversationScrollIntent;
  /** Keep CopilotChat off StickToBottom after a branch-arrow switch. */
  lockAutoScrollNone: boolean;
};

let state: ConversationScrollState = {
  intent: { kind: "bottom" },
  lockAutoScrollNone: false,
};

const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot() {
  return state;
}

export function setConversationScrollIntent(intent: ConversationScrollIntent) {
  state = {
    intent,
    lockAutoScrollNone: intent.kind === "preserve" ? true : false,
  };
  emit();
}

export function peekConversationScrollIntent(): ConversationScrollIntent {
  return state.intent;
}

export function consumeConversationScrollIntent(): ConversationScrollIntent {
  const intent = state.intent;
  state = {
    ...state,
    intent: { kind: "bottom" },
  };
  emit();
  return intent;
}

export function isChatAutoScrollLockedNone(): boolean {
  return state.lockAutoScrollNone;
}

export function useChatAutoScrollMode(): "pin-to-bottom" | "none" {
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  return snapshot.lockAutoScrollNone ? "none" : "pin-to-bottom";
}
