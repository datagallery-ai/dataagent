"use client";

import { useAgent, useCopilotKit } from "@copilotkit/react-core/v2";
import { useEffect, useMemo, useRef } from "react";
import { buildAgentRunStatePatch, hasCapability, mergeRunForwardedPropsWithCommand } from "../../data-task-state";
import { useDataTaskChatInputBindings } from "./DataTaskChatInputBindingsContext";
import { canResumeRestoredInteraction } from "../../collaboration-recap";
import { useThreadCollaborationResponsesForChat } from "./collaboration-responses";
import {
  AskUserPrompt,
  parseInterruptValue,
  SubmitPlanPrompt,
  type MastraInterrupt,
} from "./CollaborationInterruptHandler";
import {
  removeRestoredInterrupt,
  useRestoredInterrupts,
} from "./restored-interrupts";
import {
  clearPendingCollaborationInterrupt,
  setPendingCollaborationInterrupt,
  usePendingCollaborationInterrupt,
} from "./pending-collaboration-interrupt";

function toMastraInterrupt(value: unknown): MastraInterrupt | null {
  return parseInterruptValue(value);
}

/** Resume UI for pending HITL interactions restored via REST (refresh / session switch). */
export function RestoredInterruptHandler({
  agentId,
  threadId,
  capabilitiesReady,
}: {
  agentId: string;
  threadId: string;
  capabilitiesReady: boolean;
}) {
  const { copilotkit } = useCopilotKit();
  const { agent } = useAgent({ agentId });
  const { getRunForwardedProps } = useDataTaskChatInputBindings();
  const collaborationResponses = useThreadCollaborationResponsesForChat(threadId);
  const restoredInterrupts = useRestoredInterrupts(threadId);
  const livePending = usePendingCollaborationInterrupt(threadId);
  const submittingRef = useRef(false);

  const pending = useMemo(() => {
    if (!capabilitiesReady || livePending?.source === "live") {
      return undefined;
    }
    return restoredInterrupts.find((record) =>
      canResumeRestoredInteraction({
        toolCallId: record.toolCallId,
        collaborationResponses,
      }),
    );
  }, [capabilitiesReady, collaborationResponses, livePending?.source, restoredInterrupts]);

  const interrupt = pending ? toMastraInterrupt(pending.interruptEvent) : null;

  const canResume =
    capabilitiesReady &&
    hasCapability("interaction.resume") &&
    pending
      ? canResumeRestoredInteraction({
          toolCallId: pending.toolCallId,
          collaborationResponses,
        })
      : false;

  const element = useMemo(() => {
    if (!pending || !interrupt?.toolName) {
      return null;
    }

    const submitRestored = (response: unknown) => {
      if (submittingRef.current || !canResume) {
        return;
      }
      submittingRef.current = true;
      removeRestoredInterrupt(threadId, pending.toolCallId);
      clearPendingCollaborationInterrupt(threadId, "restored");
      const forwardedProps = mergeRunForwardedPropsWithCommand(
        getRunForwardedProps(),
        {
          resume: response,
          interruptEvent: pending.interruptEvent,
        },
      );
      agent.setState(buildAgentRunStatePatch(forwardedProps, agent.state));
      void copilotkit.runAgent({
        agent,
        forwardedProps,
      });
    };

    if (interrupt.toolName === "submit_plan") {
      return (
        <SubmitPlanPrompt
          interrupt={interrupt}
          onSubmit={submitRestored}
          threadId={threadId}
          agentId={agentId}
          canResume={canResume}
        />
      );
    }

    return (
      <AskUserPrompt
        interrupt={interrupt}
        onSubmit={submitRestored}
        threadId={threadId}
        agentId={agentId}
        canResume={canResume}
      />
    );
  }, [
    agent,
    agentId,
    canResume,
    copilotkit,
    interrupt,
    pending,
    threadId,
  ]);

  useEffect(() => {
    if (!element || !pending || livePending?.source === "live") {
      clearPendingCollaborationInterrupt(threadId, "restored");
      return;
    }

    setPendingCollaborationInterrupt({
      threadId,
      toolCallId: pending.toolCallId,
      element,
      source: "restored",
    });

    return () => {
      clearPendingCollaborationInterrupt(threadId, "restored");
    };
  }, [element, livePending?.source, pending, threadId]);

  return null;
}
