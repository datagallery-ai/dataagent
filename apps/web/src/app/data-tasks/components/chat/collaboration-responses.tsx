"use client";

import {
  CopilotChatAssistantMessage,
  useCopilotKit,
} from "@copilotkit/react-core/v2";
import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import type { Message } from "@ag-ui/core";
import {
  collaborationResponseLayout,
  formatCollaborationResponseDisplay,
  type CollaborationToolName,
} from "./collaboration-response-display";

export type CollaborationResponseRecord = {
  id: string;
  threadId: string;
  toolCallId: string;
  toolName: CollaborationToolName;
  question?: string;
  /** Original plan text shown for submit_plan, kept for read-only recap. */
  plan?: string;
  displayText: string;
  createdAt: number;
  /** Assistant message that streamed before the collaboration tool suspended. */
  assistantMessageId?: string;
};

type StoreState = Record<string, CollaborationResponseRecord[]>;

const EMPTY_RESPONSES: CollaborationResponseRecord[] = [];

let storeState: StoreState = {};
const listeners = new Set<() => void>();

function emitChange() {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot() {
  return storeState;
}

function responsesForThread(threadId: string): CollaborationResponseRecord[] {
  return storeState[threadId] ?? EMPTY_RESPONSES;
}

function recordResponse(entry: CollaborationResponseRecord) {
  const current = storeState[entry.threadId] ?? EMPTY_RESPONSES;
  if (current.some((item) => item.toolCallId === entry.toolCallId)) {
    return;
  }
  storeState = {
    ...storeState,
    [entry.threadId]: [...current, entry],
  };
  emitChange();
}

export function getCollaborationResponsesForThread(
  threadId: string,
): CollaborationResponseRecord[] {
  return responsesForThread(threadId);
}

export function hydrateCollaborationResponses(
  entries: Array<Omit<CollaborationResponseRecord, "id" | "createdAt">>,
): void {
  for (const entry of entries) {
    recordResponse({
      ...entry,
      id: `${entry.threadId}:${entry.toolCallId}`,
      createdAt: Date.now(),
    });
  }
}

import { CollaborationPendingInterruptSlot } from "./CollaborationPendingInterruptSlot";

export { shouldShowCollaborationRecapOnMessage } from "../../collaboration-recap";

export { formatCollaborationResponseDisplay };


type CollaborationResponsesContextValue = {
  recordResponse: (entry: Omit<CollaborationResponseRecord, "id" | "createdAt">) => void;
  getResponsesForThread: (threadId: string) => CollaborationResponseRecord[];
};

const CollaborationResponsesContext =
  createContext<CollaborationResponsesContextValue | null>(null);

export function CollaborationResponsesProvider({ children }: { children: ReactNode }) {
  const record = useCallback(
    (entry: Omit<CollaborationResponseRecord, "id" | "createdAt">) => {
      recordResponse({
        ...entry,
        id: `${entry.threadId}:${entry.toolCallId}`,
        createdAt: Date.now(),
      });
    },
    [],
  );

  const value = useMemo(
    () => ({
      recordResponse: record,
      getResponsesForThread: responsesForThread,
    }),
    [record],
  );

  return (
    <CollaborationResponsesContext.Provider value={value}>
      {children}
    </CollaborationResponsesContext.Provider>
  );
}

export function useCollaborationResponses() {
  const context = useContext(CollaborationResponsesContext);
  if (!context) {
    throw new Error("useCollaborationResponses must be used within CollaborationResponsesProvider");
  }
  return context;
}

function useThreadCollaborationResponses(threadId: string | undefined) {
  const store = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  if (!threadId) return EMPTY_RESPONSES;
  return store[threadId] ?? EMPTY_RESPONSES;
}

export function useThreadCollaborationResponsesForChat(threadId: string | undefined) {
  return useThreadCollaborationResponses(threadId);
}

function looksLikeToolName(label: string): boolean {
  return /^[a-z][a-z0-9]*(_[a-z0-9]+)+$/u.test(label.trim());
}

function PlanMarkdown({ content }: { content: string }) {
  return (
    <div className="mt-2 max-h-60 overflow-auto rounded-lg bg-surface p-2.5 text-sm leading-6 text-foreground [&_code]:rounded [&_code]:bg-surface-subtle [&_code]:px-1 [&_ol]:my-1 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-1 [&_ul]:my-1 [&_ul]:list-disc [&_ul]:pl-5">
      <CopilotChatAssistantMessage.MarkdownRenderer content={content} />
    </div>
  );
}

export function CollaborationChoiceBubble({
  response,
  inline = false,
}: {
  response: CollaborationResponseRecord;
  inline?: boolean;
}) {
  const mono = looksLikeToolName(response.displayText);
  const recapLabel = response.toolName === "submit_plan" ? "approved plan" : "answered question";
  const layout = collaborationResponseLayout(response.toolName);
  const recapClass =
    layout.recapSide === "assistant"
      ? "copilotKitMessage copilotKitAssistantMessage flex flex-col items-start"
      : "copilotKitMessage copilotKitUserMessage flex flex-col items-end";

  if (inline) {
    return (
      <div className="rounded-xl border border-border bg-surface px-3 py-2.5 shadow-[var(--shadow-card)]">
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-light">
          My choice
        </div>
        <div className={`text-sm leading-6 text-foreground ${mono ? "font-mono tracking-tight" : ""}`}>
          {response.displayText}
        </div>
      </div>
    );
  }

  return (
    <div data-copilotkit className="mb-3 pt-1">
      <div className={recapClass}>
        <div className="w-full rounded-xl border border-border bg-surface px-3 py-2.5 text-left shadow-[var(--shadow-card)]">
          {response.question ? (
            <div className="mb-2 text-xs leading-5 text-muted">
              <span className="font-medium text-muted-light">{recapLabel}：</span>
              {response.question}
            </div>
          ) : null}
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex h-5 w-5 items-center justify-center rounded-full border border-border bg-surface-subtle text-muted">
              <svg viewBox="0 0 20 20" className="h-3 w-3" fill="currentColor" aria-hidden>
                <path d="M10 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM4 17v-1a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v1H4Z" />
              </svg>
            </span>
            <span className="text-[11px] font-semibold text-muted">
              Your choice was applied
            </span>
            <span className={`rounded-md bg-surface-subtle px-2 py-0.5 text-xs text-foreground ${mono ? "font-mono tracking-tight" : ""}`}>
              {response.displayText}
            </span>
          </div>
          {response.plan && layout.planRenderer === "markdown" ? (
            <PlanMarkdown content={response.plan} />
          ) : null}
        </div>
      </div>
    </div>
  );
}

export function CollaborationResponseAfterMessage({
  message,
  position,
}: {
  message: Message;
  position: "before" | "after";
  runId: string;
  messageIndex: number;
  messageIndexInRun: number;
  numberOfMessagesInRun: number;
  agentId: string;
  stateSnapshot: unknown;
}) {
  // Recap is rendered inline from StepAssistantMessage (between HITL step and answer).
  void message;
  void position;
  return null;
}

export function CollaborationPendingInterruptAfterMessage({
  message,
  position,
  messageIndexInRun,
  numberOfMessagesInRun,
}: {
  message: Message;
  position: "before" | "after";
  runId: string;
  messageIndex: number;
  messageIndexInRun: number;
  numberOfMessagesInRun: number;
  agentId: string;
  stateSnapshot: unknown;
}) {
  // Fallback when suspend lands before any assistant message is rendered.
  if (position !== "after") {
    return null;
  }
  const isLastInRun = messageIndexInRun === numberOfMessagesInRun - 1;
  if (!isLastInRun || message.role === "assistant") {
    return null;
  }
  return <CollaborationPendingInterruptSlot message={message} />;
}

/** Single custom renderer: CopilotKit stops at the first registered renderer. */
export function CollaborationAfterMessageRenderer(
  props: Parameters<typeof CollaborationResponseAfterMessage>[0],
) {
  return (
    <>
      <CollaborationResponseAfterMessage {...props} />
      <CollaborationPendingInterruptAfterMessage {...props} />
    </>
  );
}

export function CollaborationResponseBridge() {
  const { copilotkit } = useCopilotKit();

  useEffect(() => {
    const legacyRenderers = new Set([
      CollaborationResponseAfterMessage,
      CollaborationPendingInterruptAfterMessage,
    ]);
    const existing = copilotkit.renderCustomMessages.filter(
      (entry) => !legacyRenderers.has(entry.render as typeof CollaborationResponseAfterMessage),
    );
    const hasCombined = existing.some(
      (entry) => entry.render === CollaborationAfterMessageRenderer,
    );
    if (hasCombined) {
      return;
    }

    copilotkit.setRenderCustomMessages([
      ...existing,
      {
        render: CollaborationAfterMessageRenderer,
      },
    ]);

    return () => {
      copilotkit.setRenderCustomMessages([...existing]);
    };
  }, [copilotkit]);

  return null;
}
