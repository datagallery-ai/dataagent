"use client";

import { useAgent, useCopilotChatConfiguration } from "@copilotkit/react-core/v2";
import type { Message } from "@ag-ui/core";
import { messageHostsPendingCollaborationSlot } from "../../collaboration-recap";
import { useLiveRun } from "../../use-data-foundry-run";
import { usePendingCollaborationInterrupt } from "./pending-collaboration-interrupt";

/** Inline HITL card anchored to the collaboration step / last message. */
export function CollaborationPendingInterruptSlot({
  message,
}: {
  message: Pick<Message, "id" | "role">;
}) {
  const chatConfig = useCopilotChatConfiguration();
  const threadId = chatConfig?.threadId;
  const { agent } = useAgent({ agentId: chatConfig?.agentId ?? "dataFoundry" });
  const { liveRun } = useLiveRun(threadId);
  const pendingInterrupt = usePendingCollaborationInterrupt(threadId);
  const allMessages = agent.messages ?? [];

  if (!pendingInterrupt) {
    return null;
  }

  const anchored = messageHostsPendingCollaborationSlot(
    message,
    pendingInterrupt.toolCallId,
    allMessages,
    liveRun,
    liveRun.runStatus,
  );
  if (!anchored) {
    return null;
  }

  return (
    <div data-copilotkit className="mb-3 pt-1">
      {pendingInterrupt.element}
    </div>
  );
}
