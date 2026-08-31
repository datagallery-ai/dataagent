"use client";

import { useAgent, useCopilotChatConfiguration } from "@copilotkit/react-core/v2";
import { useLayoutEffect, useRef } from "react";
import {
  restoreCopilotChatScrollTop,
  restoreCopilotChatScrollTopWithRetries,
  scrollCopilotChatToBottomWithRetries,
} from "../../chat-scroll";
import {
  consumeConversationScrollIntent,
  type ConversationScrollIntent,
} from "../../conversation-branch-scroll";
import { useConversationRestoreGate } from "../../use-data-foundry-run";

/**
 * After a historical thread is restored (switch session or full page refresh),
 * CopilotKit's pin-to-bottom does not always follow batch-loaded messages.
 * Pin the chat viewport to the latest message once restore settles — unless the
 * navigation came from the branch switcher, which restores the prior scrollTop
 * and keeps autoScroll disabled so StickToBottom cannot fight the preserve.
 */
export function SessionConversationScrollRestore({
  agentId,
}: {
  agentId: string;
}) {
  const chatConfig = useCopilotChatConfiguration();
  const threadId = chatConfig?.threadId;
  const { agent } = useAgent({ agentId });
  const { isThreadRestoring } = useConversationRestoreGate();
  const isRestoringConversation = isThreadRestoring(threadId);
  const prevRestoringRef = useRef(isRestoringConversation);
  const activeThreadRef = useRef<string | null>(null);
  const pendingScrollRef = useRef(false);
  const pendingIntentRef = useRef<ConversationScrollIntent>({ kind: "bottom" });
  const cancelScrollRef = useRef<(() => void) | null>(null);

  useLayoutEffect(() => {
    cancelScrollRef.current?.();
    cancelScrollRef.current = null;

    if (!threadId) {
      return;
    }

    if (activeThreadRef.current !== threadId) {
      activeThreadRef.current = threadId;
      pendingScrollRef.current = true;
      pendingIntentRef.current = consumeConversationScrollIntent();
    }

    if (isRestoringConversation) {
      prevRestoringRef.current = isRestoringConversation;
      return;
    }

    const messageCount = agent.messages?.length ?? 0;
    const restoreCompleted =
      prevRestoringRef.current && !isRestoringConversation;

    if (messageCount > 0 && (restoreCompleted || pendingScrollRef.current)) {
      const intent = pendingIntentRef.current;
      pendingScrollRef.current = false;
      pendingIntentRef.current = { kind: "bottom" };

      if (intent.kind === "preserve") {
        // Apply once synchronously before paint, then keep reinforcing while
        // restored messages finish laying out.
        restoreCopilotChatScrollTop(intent.scrollTop);
        cancelScrollRef.current = restoreCopilotChatScrollTopWithRetries({
          scrollTop: intent.scrollTop,
        });
      } else {
        cancelScrollRef.current = scrollCopilotChatToBottomWithRetries();
      }
    }

    prevRestoringRef.current = isRestoringConversation;

    return () => {
      cancelScrollRef.current?.();
      cancelScrollRef.current = null;
    };
  }, [agent.messages, isRestoringConversation, threadId]);

  return null;
}
