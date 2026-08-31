"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type SetStateAction,
} from "react";
import {
  useAgent,
  useCopilotChatConfiguration,
  UseAgentUpdate,
} from "@copilotkit/react-core/v2";
import type { BaseEvent } from "@ag-ui/client";
import {
  accumulateSessionUsage,
  createInitialLiveRun,
  createInitialSessionUsage,
  deriveSegmentRunUsage,
  reduceLiveRunEvent,
  shouldIgnoreIncomingRunError,
  shouldSkipAgUiReplayDuringRestore,
  type LiveRun,
  type LiveRunStatus,
  type SessionUsageStats,
} from "./live-run-state";
import { formatRunErrorMessage } from "./run-error-message";
import {
  isCollaborationEchoUserMessage,
  normalizeUserQuestionText,
} from "./conversation-restore";
import { reconcileSuspendedLiveRunState } from "./collaboration-recap";
import { getCollaborationResponsesForThread } from "./components/chat/collaboration-responses";

type LiveRunThreadSnapshot = {
  liveRun: LiveRun;
  sessionUsage: SessionUsageStats;
  latestQuestion?: string;
  runningThreadIds: ReadonlySet<string>;
};

type LiveRunStoreContextValue = {
  liveRunsByThreadId: Readonly<Record<string, LiveRun>>;
  sessionUsageByThreadId: Readonly<Record<string, SessionUsageStats>>;
  latestQuestionByThreadId: Readonly<Record<string, string | undefined>>;
  runningThreadIds: ReadonlySet<string>;
};

type LiveRunSetters = {
  setLiveRunForThread: (threadId: string | undefined, action: SetStateAction<LiveRun>) => void;
  setSessionUsageForThread: (
    threadId: string | undefined,
    action: SetStateAction<SessionUsageStats>,
  ) => void;
  setLatestQuestionForThread: (
    threadId: string | undefined,
    action: SetStateAction<string | undefined>,
  ) => void;
  syncRunningThreadStatus: (threadId: string | undefined, status: LiveRunStatus) => void;
};

type ConversationRestoreGate = {
  /** Threads currently loading persisted conversation history. */
  restoringThreadIds: ReadonlySet<string>;
  /** Threads that completed at least one restore cycle (including empty history). */
  restoredThreadIds: ReadonlySet<string>;
  isThreadRestoring: (threadId: string | null | undefined) => boolean;
  isThreadRestored: (threadId: string | null | undefined) => boolean;
  setThreadRestoring: (threadId: string, restoring: boolean) => void;
  markThreadRestored: (threadId: string) => void;
};

const emptyLiveRun = createInitialLiveRun();
const emptySessionUsage = createInitialSessionUsage();

const LiveRunContext = createContext<LiveRunStoreContextValue | null>(null);
const LiveRunSettersContext = createContext<LiveRunSetters | null>(null);
const ConversationRestoreGateContext = createContext<ConversationRestoreGate | null>(null);

function resolveStateAction<T>(current: T, action: SetStateAction<T>): T {
  return typeof action === "function"
    ? (action as (current: T) => T)(current)
    : action;
}

function normalizeUserQuestion(text: string): string | undefined {
  return normalizeUserQuestionText(text);
}

function extractLatestUserQuestion(messages: unknown): string | undefined {
  if (!Array.isArray(messages)) return undefined;
  let fallback: string | undefined;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index] as Record<string, unknown> | null;
    if (!message || message.role !== "user") continue;
    const content = message.content;
    let text: string | undefined;
    if (typeof content === "string") {
      text = normalizeUserQuestion(content);
    } else if (Array.isArray(content)) {
      text = normalizeUserQuestion(
        content
          .map((part) => {
            if (part && typeof part === "object" && (part as { type?: unknown }).type === "text") {
              return String((part as { text?: unknown }).text ?? "");
            }
            return "";
          })
          .join(""),
      );
    }
    if (!text) continue;
    fallback ??= text;
    if (!isCollaborationEchoUserMessage(text)) {
      return text;
    }
  }
  return fallback;
}

function syncRunningThreadIdSet(
  current: Set<string>,
  threadId: string,
  status: LiveRunStatus,
): Set<string> {
  const active = status === "running" || status === "suspended";
  const has = current.has(threadId);
  if (active === has) {
    return current;
  }
  const next = new Set(current);
  if (active) {
    next.add(threadId);
  } else {
    next.delete(threadId);
  }
  return next;
}

export function LiveRunProvider({ children }: { children: ReactNode }) {
  const [liveRunsByThreadId, setLiveRunsByThreadId] = useState<Record<string, LiveRun>>({});
  const [sessionUsageByThreadId, setSessionUsageByThreadId] = useState<
    Record<string, SessionUsageStats>
  >({});
  const [latestQuestionByThreadId, setLatestQuestionByThreadId] = useState<
    Record<string, string | undefined>
  >({});
  const [runningThreadIds, setRunningThreadIds] = useState<Set<string>>(() => new Set());
  const [restoringThreadIds, setRestoringThreadIds] = useState<Set<string>>(() => new Set());
  const [restoredThreadIds, setRestoredThreadIds] = useState<Set<string>>(() => new Set());
  const prevRunStatusByThreadRef = useRef<Record<string, LiveRunStatus>>({});

  const setThreadRestoring = useCallback((threadId: string, restoring: boolean) => {
    setRestoringThreadIds((current) => {
      const has = current.has(threadId);
      if (restoring === has) {
        return current;
      }
      const next = new Set(current);
      if (restoring) {
        next.add(threadId);
      } else {
        next.delete(threadId);
      }
      return next;
    });
  }, []);

  const markThreadRestored = useCallback((threadId: string) => {
    setRestoredThreadIds((current) => {
      if (current.has(threadId)) {
        return current;
      }
      const next = new Set(current);
      next.add(threadId);
      return next;
    });
  }, []);

  const isThreadRestoring = useCallback(
    (threadId: string | null | undefined) =>
      Boolean(threadId && restoringThreadIds.has(threadId)),
    [restoringThreadIds],
  );

  const isThreadRestored = useCallback(
    (threadId: string | null | undefined) =>
      Boolean(threadId && restoredThreadIds.has(threadId)),
    [restoredThreadIds],
  );

  const setLiveRunForThread = useCallback(
    (threadId: string | undefined, action: SetStateAction<LiveRun>) => {
      if (!threadId) return;
      setLiveRunsByThreadId((current) => {
        const previous = current[threadId] ?? createInitialLiveRun();
        const next = resolveStateAction(previous, action);
        if (next === previous && current[threadId]) {
          return current;
        }
        return { ...current, [threadId]: next };
      });
    },
    [],
  );

  const setSessionUsageForThread = useCallback(
    (threadId: string | undefined, action: SetStateAction<SessionUsageStats>) => {
      if (!threadId) return;
      setSessionUsageByThreadId((current) => {
        const previous = current[threadId] ?? createInitialSessionUsage();
        const next = resolveStateAction(previous, action);
        if (next === previous && current[threadId]) {
          return current;
        }
        return { ...current, [threadId]: next };
      });
    },
    [],
  );

  const setLatestQuestionForThread = useCallback(
    (threadId: string | undefined, action: SetStateAction<string | undefined>) => {
      if (!threadId) return;
      setLatestQuestionByThreadId((current) => {
        const previous = current[threadId];
        const next = resolveStateAction(previous, action);
        if (next === previous) {
          return current;
        }
        if (next === undefined) {
          const { [threadId]: _removed, ...rest } = current;
          return rest;
        }
        return { ...current, [threadId]: next };
      });
    },
    [],
  );

  const syncRunningThreadStatus = useCallback(
    (threadId: string | undefined, status: LiveRunStatus) => {
      if (!threadId) {
        return;
      }
      setRunningThreadIds((current) => syncRunningThreadIdSet(current, threadId, status));
    },
    [],
  );

  const setters = useMemo(
    () => ({
      setLiveRunForThread,
      setSessionUsageForThread,
      setLatestQuestionForThread,
      syncRunningThreadStatus,
    }),
    [
      setLatestQuestionForThread,
      setLiveRunForThread,
      setSessionUsageForThread,
      syncRunningThreadStatus,
    ],
  );

  const restoreGate = useMemo(
    () => ({
      restoringThreadIds,
      restoredThreadIds,
      isThreadRestoring,
      isThreadRestored,
      setThreadRestoring,
      markThreadRestored,
    }),
    [
      isThreadRestored,
      isThreadRestoring,
      markThreadRestored,
      restoredThreadIds,
      restoringThreadIds,
      setThreadRestoring,
    ],
  );

  useLayoutEffect(() => {
    Object.entries(liveRunsByThreadId).forEach(([threadId, liveRun]) => {
      const previous = prevRunStatusByThreadRef.current[threadId] ?? "idle";
      const current = liveRun.runStatus;
      if (previous === "running" && current === "completed") {
        setSessionUsageForThread(threadId, (stats) =>
          accumulateSessionUsage(stats, deriveSegmentRunUsage(liveRun), "completed"),
        );
      } else if (previous === "running" && current === "failed") {
        setSessionUsageForThread(threadId, (stats) =>
          accumulateSessionUsage(stats, deriveSegmentRunUsage(liveRun), "failed"),
        );
      }
      prevRunStatusByThreadRef.current[threadId] = current;
    });
  }, [liveRunsByThreadId, setSessionUsageForThread]);

  const value = useMemo(
    () => ({
      liveRunsByThreadId,
      sessionUsageByThreadId,
      latestQuestionByThreadId,
      runningThreadIds,
    }),
    [
      latestQuestionByThreadId,
      liveRunsByThreadId,
      runningThreadIds,
      sessionUsageByThreadId,
    ],
  );

  return (
    <LiveRunSettersContext.Provider value={setters}>
      <ConversationRestoreGateContext.Provider value={restoreGate}>
        <LiveRunContext.Provider value={value}>
          {children}
        </LiveRunContext.Provider>
      </ConversationRestoreGateContext.Provider>
    </LiveRunSettersContext.Provider>
  );
}

export function useLiveRun(threadId?: string | null): LiveRunThreadSnapshot {
  const ctx = useContext(LiveRunContext);
  if (!ctx) {
    throw new Error("useLiveRun must be used within LiveRunProvider");
  }
  const chatConfig = useCopilotChatConfiguration();
  const resolvedThreadId = threadId ?? chatConfig?.threadId ?? null;
  return {
    liveRun: resolvedThreadId
      ? ctx.liveRunsByThreadId[resolvedThreadId] ?? emptyLiveRun
      : emptyLiveRun,
    sessionUsage: resolvedThreadId
      ? ctx.sessionUsageByThreadId[resolvedThreadId] ?? emptySessionUsage
      : emptySessionUsage,
    latestQuestion: resolvedThreadId
      ? ctx.latestQuestionByThreadId[resolvedThreadId]
      : undefined,
    runningThreadIds: ctx.runningThreadIds,
  };
}

export function useLiveRunSetters(): LiveRunSetters {
  const setters = useContext(LiveRunSettersContext);
  if (!setters) {
    throw new Error("useLiveRunSetters must be used within LiveRunProvider");
  }
  return setters;
}

export function useConversationRestoreGate(): ConversationRestoreGate {
  const gate = useContext(ConversationRestoreGateContext);
  if (!gate) {
    throw new Error("useConversationRestoreGate must be used within LiveRunProvider");
  }
  return gate;
}

/**
 * Subscribes to AG-UI events for a thread. Must render as a sibling
 * **before** `<CopilotChat>` inside `<CopilotChatConfigurationProvider>` so
 * its effect runs before connectAgent replays historical events.
 */
export function LiveRunEventSubscriber({
  agentId,
  threadId,
}: {
  agentId: string;
  threadId?: string;
}) {
  const setters = useContext(LiveRunSettersContext);
  if (!setters) {
    throw new Error("LiveRunEventSubscriber requires LiveRunProvider");
  }
  const {
    setLiveRunForThread,
    setLatestQuestionForThread,
    syncRunningThreadStatus,
  } = setters;
  const { isThreadRestoring } = useConversationRestoreGate();
  const isRestoringConversation = isThreadRestoring(threadId);
  const isRestoringConversationRef = useRef(isRestoringConversation);
  isRestoringConversationRef.current = isRestoringConversation;

  const { agent } = useAgent({
    agentId,
    updates: [
      UseAgentUpdate.OnMessagesChanged,
      UseAgentUpdate.OnStateChanged,
      UseAgentUpdate.OnRunStatusChanged,
    ],
  });

  useEffect(() => {
    const applyEvent = (event: BaseEvent) => {
      if (!threadId) {
        return;
      }
      setLiveRunForThread(threadId, (current) => {
        if (
          isRestoringConversationRef.current &&
          shouldSkipAgUiReplayDuringRestore(current, event as { type?: string; [key: string]: unknown })
        ) {
          return current;
        }
        const next = reduceLiveRunEvent(current, event);
        const reconciled =
          event.type === "RUN_FINISHED" || event.type === "STATE_SNAPSHOT"
            ? reconcileSuspendedLiveRunState(
                next,
                getCollaborationResponsesForThread(threadId),
              )
            : next;
        syncRunningThreadStatus(threadId, reconciled.runStatus);
        return reconciled;
      });
    };

    const subscription = agent.subscribe({
      onEvent: ({ event }) => {
        applyEvent(event as BaseEvent);
      },
      onRunStartedEvent: ({ event }) => {
        applyEvent(event as BaseEvent);
      },
      onRunFinishedEvent: ({ event }) => {
        applyEvent(event as BaseEvent);
      },
      onRunErrorEvent: ({ event }) => {
        applyEvent(event as BaseEvent);
      },
      onActivitySnapshotEvent: ({ event }) => {
        applyEvent(event as BaseEvent);
      },
      onActivityDeltaEvent: ({ event }) => {
        applyEvent(event as BaseEvent);
      },
      onStateSnapshotEvent: ({ event }) => {
        applyEvent(event as BaseEvent);
      },
      onStateDeltaEvent: ({ event }) => {
        applyEvent(event as BaseEvent);
      },
      onToolCallStartEvent: ({ event }) => {
        applyEvent(event as BaseEvent);
      },
      onToolCallArgsEvent: ({ event }) => {
        applyEvent(event as BaseEvent);
      },
      onToolCallEndEvent: ({ event }) => {
        applyEvent(event as BaseEvent);
      },
      onToolCallResultEvent: ({ event }) => {
        applyEvent(event as BaseEvent);
      },
      onCustomEvent: ({ event }) => {
        applyEvent(event as BaseEvent);
      },
      onRunFailed: ({ error }) => {
        if (isRestoringConversationRef.current) {
          return;
        }
        if (!threadId) {
          return;
        }
        setLiveRunForThread(threadId, (current) => {
          if (shouldIgnoreIncomingRunError(current)) {
            return current;
          }
          const next = reduceLiveRunEvent(current, {
            type: "RUN_ERROR",
            message: error.message,
          });
          syncRunningThreadStatus(threadId, next.runStatus);
          return next;
        });
      },
    });

    return () => subscription.unsubscribe();
  }, [agent, setLiveRunForThread, syncRunningThreadStatus, threadId]);

  useEffect(() => {
    if (isRestoringConversationRef.current) {
      return;
    }
    const state = agent.state as Record<string, unknown> | undefined;
    if (!state || Object.keys(state).length === 0) return;
    if (!threadId) return;

    setLiveRunForThread(threadId, (current) => {
      const next = reduceLiveRunEvent(current, {
        type: "STATE_SNAPSHOT",
        snapshot: state,
      });
      const reconciled = reconcileSuspendedLiveRunState(
        next,
        getCollaborationResponsesForThread(threadId),
      );
      syncRunningThreadStatus(threadId, reconciled.runStatus);
      return reconciled;
    });
  }, [
    agent.state,
    setLiveRunForThread,
    isRestoringConversation,
    syncRunningThreadStatus,
    threadId,
  ]);

  useEffect(() => {
    const onError = (event: Event) => {
      const detail = (
        event as CustomEvent<{
          message?: string;
          threadId?: string | null;
          source?: "client" | "runtime";
        }>
      ).detail;
      if (!threadId || detail?.threadId !== threadId) {
        return;
      }
      const isClientError = detail?.source === "client";
      setLiveRunForThread(threadId, (current) => {
        if (!isClientError && shouldIgnoreIncomingRunError(current)) {
          return current;
        }
        // After a finished run, still surface client-side send failures without
        // rewriting the terminal status (stale AG-UI RUN_ERROR stays ignored).
        if (
          isClientError &&
          (current.runStatus === "completed" || current.runStatus === "canceled")
        ) {
          return {
            ...current,
            errorMessage: formatRunErrorMessage(detail?.message),
          };
        }
        const next = reduceLiveRunEvent(current, {
          type: "RUN_ERROR",
          message: detail?.message,
        });
        syncRunningThreadStatus(threadId, next.runStatus);
        return next;
      });
    };

    window.addEventListener("datafoundry-run-error", onError);
    return () => {
      window.removeEventListener("datafoundry-run-error", onError);
    };
  }, [setLiveRunForThread, syncRunningThreadStatus, threadId]);

  const latestQuestion = extractLatestUserQuestion(
    (agent as { messages?: unknown }).messages,
  );
  useEffect(() => {
    if (isRestoringConversationRef.current) {
      return;
    }
    setLatestQuestionForThread(threadId, latestQuestion);
  }, [isRestoringConversation, latestQuestion, setLatestQuestionForThread, threadId]);

  return null;
}
