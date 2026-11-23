"use client";

import { useAgent, useCopilotChatConfiguration } from "@copilotkit/react-core/v2";
import { useEffect, useLayoutEffect, useRef } from "react";
import type { Message } from "@ag-ui/core";
import { configApi } from "../../../../lib/config-api/client";
import { getRuntimeCapabilities } from "../../../../lib/config-api/capabilities";
import {
  isIgnorableConversationRestoreError,
  isConversationRestoreRunActive,
  liveRunFromNativeMessages,
  mergeStoredLiveRun,
  readStoredLiveRun,
  shouldApplyNativeSession,
} from "../../conversation-restore";
import {
  clearRestoredInterrupts,
} from "./restored-interrupts";
import { clearPendingCollaborationInterrupt } from "./pending-collaboration-interrupt";
import {
  useConversationRestoreGate,
  useLiveRun,
  useLiveRunSetters,
} from "../../use-data-gallery-run";
import { clearConversationBranchSnapshot } from "../../conversation-branch-store";
import { setSessionNotice } from "./session-notice";

export function SessionConversationRestore({
  agentId,
  capabilitiesReady,
}: {
  agentId: string;
  capabilitiesReady: boolean;
}) {
  const chatConfig = useCopilotChatConfiguration();
  const threadId = chatConfig?.threadId;
  const { agent } = useAgent({ agentId });
  const { liveRun } = useLiveRun(threadId);
  const { setLiveRunForThread } = useLiveRunSetters();
  const { setThreadRestoring, markThreadRestored } = useConversationRestoreGate();
  const fetchGenerationRef = useRef(0);
  const prevThreadIdRef = useRef<string | undefined>(undefined);
  const messageReplacementThreadRef = useRef<string | undefined>(undefined);
  const agentRef = useRef(agent);
  agentRef.current = agent;
  const restoreRunActive = isConversationRestoreRunActive({
    agentIsRunning: Boolean(agent.isRunning),
    liveRunStatus: liveRun.runStatus,
  });

  useLayoutEffect(() => {
    if (!threadId) {
      return;
    }
    // Gate while capabilities load so welcome cannot paint before restore starts.
    if (!capabilitiesReady) {
      setThreadRestoring(threadId, true);
      return;
    }
    if (!getRuntimeCapabilities().conversationMemory) {
      setThreadRestoring(threadId, false);
      markThreadRestored(threadId);
      return;
    }
    const threadChanged = prevThreadIdRef.current !== threadId;
    prevThreadIdRef.current = threadId;
    if (!threadChanged) {
      return;
    }
    // Always gate the UI for this thread on first mount so welcome cannot flash
    // while history is loading — even if a live run later skips message rewrite.
    setThreadRestoring(threadId, true);
    if (restoreRunActive) {
      return;
    }
    agentRef.current.setMessages([]);
    messageReplacementThreadRef.current = threadId;
    clearRestoredInterrupts(threadId);
    clearPendingCollaborationInterrupt(threadId);
    clearConversationBranchSnapshot(threadId);
  }, [
    capabilitiesReady,
    markThreadRestored,
    restoreRunActive,
    setThreadRestoring,
    threadId,
  ]);

  useEffect(() => {
    if (!threadId || !capabilitiesReady) {
      return;
    }

    const conversationMemoryEnabled = getRuntimeCapabilities().conversationMemory;
    if (!conversationMemoryEnabled) {
      setThreadRestoring(threadId, false);
      markThreadRestored(threadId);
      return;
    }

    if (restoreRunActive) {
      fetchGenerationRef.current += 1;
      // Active streaming already owns the transcript; release the loading gate.
      setThreadRestoring(threadId, false);
      markThreadRestored(threadId);
      return;
    }

    // Cover both first mount and deferred restore after a live run settles.
    setThreadRestoring(threadId, true);
    const generation = fetchGenerationRef.current + 1;
    fetchGenerationRef.current = generation;
    let cancelled = false;

    void (async () => {
      try {
        const session = await configApi.getNativeSession(threadId);
        if (cancelled || fetchGenerationRef.current !== generation || agentRef.current.isRunning) {
          return;
        }
        setSessionNotice(threadId, session.notice);

        const currentAgent = agentRef.current;
        if (
          shouldApplyNativeSession({
            conversationMemoryEnabled,
            isRunning: restoreRunActive,
            replaceExistingMessages:
              messageReplacementThreadRef.current === threadId ||
              (currentAgent.messages?.length ?? 0) === 0,
            agentMessages: currentAgent.messages,
            messages: session.messages,
          })
        ) {
          currentAgent.setMessages(session.messages as Message[]);
          const hydrated = mergeStoredLiveRun(
            liveRunFromNativeMessages(session.messages, session.status),
            readStoredLiveRun(threadId),
          );
          if (hydrated.toolCalls.length > 0 || hydrated.runStatus !== "idle") {
            setLiveRunForThread(threadId, (current) =>
              current.toolCalls.length > 0 ? current : hydrated,
            );
          }
        }
      } catch (error) {
        if (isIgnorableConversationRestoreError(error)) {
          setSessionNotice(threadId, null);
          return;
        }
        if (typeof window !== "undefined") {
          console.warn("[conversation-restore] failed to load session history", error);
        }
      } finally {
        if (!cancelled && fetchGenerationRef.current === generation) {
          if (messageReplacementThreadRef.current === threadId) {
            messageReplacementThreadRef.current = undefined;
          }
          markThreadRestored(threadId);
          setThreadRestoring(threadId, false);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [
    capabilitiesReady,
    markThreadRestored,
    restoreRunActive,
    setThreadRestoring,
    threadId,
  ]);

  return null;
}
