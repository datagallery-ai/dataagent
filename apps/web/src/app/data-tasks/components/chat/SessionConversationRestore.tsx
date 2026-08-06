"use client";

import { useAgent, useCopilotChatConfiguration } from "@copilotkit/react-core/v2";
import { useEffect, useLayoutEffect, useRef } from "react";
import { configApi } from "../../../../lib/config-api/client";
import { getRuntimeCapabilities } from "../../../../lib/config-api/capabilities";
import {
  collaborationResponsesFromConversation,
  conversationToAgentMessages,
  hydrateLiveRunFromConversation,
  hydratePendingInteractionLiveRun,
  hydrateSessionUsageFromConversation,
  isIgnorableConversationRestoreError,
  isConversationRestoreRunActive,
  latestUserQuestionFromConversation,
  pendingInteractionsFromConversation,
  shouldRestoreConversationMessages,
} from "../../conversation-restore";
import { reconcileSuspendedLiveRunState } from "../../collaboration-recap";
import {
  getCollaborationResponsesForThread,
  hydrateCollaborationResponses,
} from "./collaboration-responses";
import {
  clearRestoredInterrupts,
  hydrateRestoredInterrupts,
} from "./restored-interrupts";
import { clearPendingCollaborationInterrupt } from "./pending-collaboration-interrupt";
import {
  useConversationRestoreGate,
  useLiveRun,
  useLiveRunSetters,
} from "../../use-data-foundry-run";
import {
  clearConversationBranchSnapshot,
  setConversationBranchSnapshot,
} from "../../conversation-branch-store";

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
  const {
    setLiveRunForThread,
    setLatestQuestionForThread,
    setSessionUsageForThread,
  } = useLiveRunSetters();
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
        const conversation = await configApi.getSessionConversation(threadId);
        if (cancelled || fetchGenerationRef.current !== generation) {
          return;
        }
        setConversationBranchSnapshot(threadId, conversation);

        const currentAgent = agentRef.current;
        if (
          shouldRestoreConversationMessages({
            conversationMemoryEnabled,
            isRunning: restoreRunActive,
            replaceExistingMessages:
              messageReplacementThreadRef.current === threadId ||
              (currentAgent.messages?.length ?? 0) === 0,
            agentMessages: currentAgent.messages,
            dto: conversation,
          })
        ) {
          const restored = conversationToAgentMessages(conversation);
          if (restored.length > 0) {
            currentAgent.setMessages(restored);
          }
        }

        const restoredCollaboration = collaborationResponsesFromConversation(
          threadId,
          conversation,
        );
        hydrateCollaborationResponses(restoredCollaboration);

        hydrateRestoredInterrupts(
          pendingInteractionsFromConversation(threadId, conversation),
        );

        setSessionUsageForThread(threadId, hydrateSessionUsageFromConversation(conversation));

        const collaborationRecords = getCollaborationResponsesForThread(threadId);
        setLiveRunForThread(threadId, (current) =>
          reconcileSuspendedLiveRunState(
            hydratePendingInteractionLiveRun(
              hydrateLiveRunFromConversation(current, conversation),
              threadId,
              conversation,
              collaborationRecords,
            ),
            collaborationRecords,
          ),
        );

        const question = latestUserQuestionFromConversation(conversation);
        if (question) {
          setLatestQuestionForThread(threadId, question);
        }
      } catch (error) {
        if (isIgnorableConversationRestoreError(error)) {
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
    setLatestQuestionForThread,
    setLiveRunForThread,
    setSessionUsageForThread,
    setThreadRestoring,
    threadId,
  ]);

  return null;
}
