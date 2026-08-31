"use client";

import { CopilotChatAssistantMessage, useAgent, useCopilotKit } from "@copilotkit/react-core/v2";
import { useCallback, useEffect, useMemo, useRef, useState, type ComponentProps } from "react";
import {
  btnPrimaryClass,
  btnSecondaryClass,
  choiceOptionClass,
  choiceOptionChevronClass,
  choiceOptionIconClass,
  panelShellClass,
  sectionLabelClass,
} from "../../ui-tokens";
import {
  formatCollaborationResponseDisplay,
  useCollaborationResponses,
  useThreadCollaborationResponsesForChat,
} from "./collaboration-responses";
import {
  canResumeCollaborationInterrupt,
  findPendingCollaborationToolCall,
} from "../../collaboration-recap";
import { buildAgentRunStatePatch, mergeRunForwardedPropsWithCommand } from "../../data-task-state";
import { useLiveRun } from "../../use-data-foundry-run";
import { useDataTaskChatInputBindings } from "./DataTaskChatInputBindingsContext";
import {
  clearPendingCollaborationInterrupt,
  setPendingCollaborationInterrupt,
} from "./pending-collaboration-interrupt";

type MastraInterrupt = {
  type?: string;
  toolCallId?: string;
  toolName?: "ask_user" | "submit_plan";
  suspendPayload?: Record<string, unknown>;
  args?: Record<string, unknown>;
};

type ChoiceOption = { label: string; value: string; description?: string };

function parseInterruptValue(value: unknown): MastraInterrupt | null {
  const raw =
    typeof value === "string"
      ? (() => {
          try {
            return JSON.parse(value) as unknown;
          } catch {
            return null;
          }
        })()
      : value;
  if (!raw || typeof raw !== "object") return null;
  return raw as MastraInterrupt;
}

function readQuestion(interrupt: MastraInterrupt): string {
  const payload = interrupt.suspendPayload;
  if (payload && typeof payload.question === "string") return payload.question;
  if (interrupt.args && typeof interrupt.args.question === "string") {
    return interrupt.args.question;
  }
  return "The agent needs more information";
}

function normalizeOption(item: unknown): ChoiceOption | null {
  if (typeof item === "string" && item.trim()) {
    const label = item.trim();
    return { label, value: label };
  }
  if (!item || typeof item !== "object") return null;
  const record = item as Record<string, unknown>;
  const label =
    typeof record.label === "string"
      ? record.label
      : typeof record.value === "string"
        ? record.value
        : null;
  if (!label) return null;
  const value =
    typeof record.value === "string" ? record.value : label;
  const description =
    typeof record.description === "string" ? record.description : undefined;
  return { label, value, description };
}

function readOptions(interrupt: MastraInterrupt): ChoiceOption[] {
  const sources = [interrupt.suspendPayload?.options, interrupt.args?.options];
  for (const raw of sources) {
    if (!Array.isArray(raw)) continue;
    const parsed = raw
      .map(normalizeOption)
      .filter((item): item is ChoiceOption => Boolean(item));
    if (parsed.length > 0) return parsed;
  }
  return [];
}

function looksLikeToolName(label: string): boolean {
  return /^[a-z][a-z0-9]*(_[a-z0-9]+)+$/u.test(label.trim());
}

function ChoiceOptionList({
  options,
  disabled,
  onSelect,
}: {
  options: ChoiceOption[];
  disabled: boolean;
  onSelect: (value: string) => void;
}) {
  return (
    <div className="mt-3 grid gap-2">
      {options.map((option, index) => {
        const mono = looksLikeToolName(option.label);

        return (
          <button
            key={`${option.value}-${index}`}
            type="button"
            disabled={disabled}
            className={[
              "hitl-choice-option group",
              choiceOptionClass,
              disabled ? "pointer-events-none opacity-60" : "",
            ].join(" ")}
            onClick={(event) => {
              event.stopPropagation();
              if (!disabled) onSelect(option.value);
            }}
          >
            <span className={choiceOptionIconClass}>{index + 1}</span>
            <span className="min-w-0 flex-1 text-left">
              <span
                className={[
                  "block text-sm font-medium text-foreground",
                  mono ? "font-mono tracking-tight" : "",
                ].join(" ")}
              >
                {option.label}
              </span>
              {option.description ? (
                <span className="mt-0.5 block text-xs leading-5 text-muted">
                  {option.description}
                </span>
              ) : null}
            </span>
            <svg
              aria-hidden="true"
              viewBox="0 0 16 16"
              className={["h-4 w-4", choiceOptionChevronClass].join(" ")}
              fill="none"
            >
              <path
                d="M6 4l4 4-4 4"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        );
      })}
    </div>
  );
}

function readPlan(interrupt: MastraInterrupt): string {
  const payload = interrupt.suspendPayload;
  if (payload && typeof payload.plan === "string") return payload.plan;
  if (interrupt.args && typeof interrupt.args.plan === "string") return interrupt.args.plan;
  return "";
}

function PlanMarkdown({ content }: { content: string }) {
  return (
    <div className="mt-3 max-h-72 overflow-auto rounded-lg bg-surface-subtle p-3 text-sm leading-6 text-foreground [&_code]:rounded [&_code]:bg-surface [&_code]:px-1 [&_ol]:my-1 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-1 [&_ul]:my-1 [&_ul]:list-disc [&_ul]:pl-5">
      <CopilotChatAssistantMessage.MarkdownRenderer content={content} />
    </div>
  );
}

function AskUserPrompt({
  interrupt,
  onSubmit,
  threadId,
  agentId,
  canResume,
}: {
  interrupt: MastraInterrupt;
  onSubmit: (response: unknown) => void;
  threadId: string;
  agentId: string;
  canResume: boolean;
}) {
  const { recordResponse } = useCollaborationResponses();
  const { agent } = useAgent({ agentId });
  const [answer, setAnswer] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const question = readQuestion(interrupt);
  const options = readOptions(interrupt);

  const submit = useCallback(
    (value: unknown) => {
      if (submitted || !canResume) return;
      setSubmitted(true);
      const assistantMessageId = [...(agent.messages ?? [])]
        .reverse()
        .find((item) => item.role === "assistant")?.id;
      if (interrupt.toolCallId) {
        recordResponse({
          threadId,
          toolCallId: interrupt.toolCallId,
          toolName: "ask_user",
          question,
          displayText: formatCollaborationResponseDisplay("ask_user", value, options),
          assistantMessageId,
        });
      }
      onSubmit(value);
    },
    [
      agent.messages,
      interrupt.toolCallId,
      options,
      question,
      onSubmit,
      recordResponse,
      submitted,
      threadId,
      canResume,
    ],
  );

  if (options.length > 0) {
    return (
      <CollaborationInterruptPanel data-testid="collaboration-interrupt-ask-user">
        <div className={sectionLabelClass}>User Collaboration</div>
        <p className="mt-1.5 text-sm font-medium leading-6 text-foreground">{question}</p>
        <ChoiceOptionList
          options={options}
          disabled={submitted}
          onSelect={(value) => submit(value)}
        />
      </CollaborationInterruptPanel>
    );
  }

  return (
    <CollaborationInterruptPanel data-testid="collaboration-interrupt-ask-user">
      <div className={sectionLabelClass}>User Collaboration</div>
      <p className="mt-1.5 text-sm font-medium text-foreground">{question}</p>
      <textarea
        value={answer}
        onChange={(event) => setAnswer(event.target.value)}
        rows={3}
        disabled={submitted}
        className="mt-3 w-full resize-none rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none transition-colors duration-150 focus:border-muted-light focus-visible:ring-2 focus-visible:ring-primary/20 disabled:opacity-60"
        placeholder="Enter your answer..."
      />
      <div className="mt-3 flex justify-end">
        <button
          type="button"
          className={btnPrimaryClass}
          disabled={submitted || !answer.trim()}
          onClick={() => submit(answer.trim())}
        >
          Submit Answer
        </button>
      </div>
    </CollaborationInterruptPanel>
  );
}

function SubmitPlanPrompt({
  interrupt,
  onSubmit,
  threadId,
  agentId,
  canResume,
}: {
  interrupt: MastraInterrupt;
  onSubmit: (response: unknown) => void;
  threadId: string;
  agentId: string;
  canResume: boolean;
}) {
  const { recordResponse } = useCollaborationResponses();
  const { agent } = useAgent({ agentId });
  const [submitted, setSubmitted] = useState(false);
  const title =
    typeof interrupt.args?.title === "string"
      ? interrupt.args.title
      : "Execution Plan Approval";
  const plan = readPlan(interrupt);

  const submit = (response: unknown) => {
    if (submitted || !canResume) return;
    setSubmitted(true);
    const assistantMessageId = [...(agent.messages ?? [])]
      .reverse()
      .find((item) => item.role === "assistant")?.id;
    if (interrupt.toolCallId) {
      recordResponse({
        threadId,
        toolCallId: interrupt.toolCallId,
        toolName: "submit_plan",
        question: title,
        plan,
        displayText: formatCollaborationResponseDisplay("submit_plan", response),
        assistantMessageId,
      });
    }
    onSubmit(response);
  };

  return (
    <CollaborationInterruptPanel data-testid="collaboration-interrupt-submit-plan">
      <p className="text-sm font-semibold text-foreground">{title}</p>
      {plan ? (
        <PlanMarkdown content={plan} />
      ) : null}
      <div className="mt-3 flex flex-wrap justify-end gap-2">
        <button
          type="button"
          className={`hitl-action-btn-secondary ${btnSecondaryClass}`}
          disabled={submitted}
          onClick={() => submit({ action: "rejected", feedback: "Needs changes" })}
        >
          Reject
        </button>
        <button
          type="button"
          className={btnPrimaryClass}
          disabled={submitted}
          onClick={() => submit({ action: "approved" })}
        >
          Approve
        </button>
      </div>
    </CollaborationInterruptPanel>
  );
}

function CollaborationInterruptPanel({
  children,
  ...props
}: ComponentProps<"div">) {
  return (
    <div
      {...props}
      data-no-tool-select
      className={[
        panelShellClass,
        "relative z-30 mb-3 mt-2 pointer-events-auto",
        props.className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {children}
    </div>
  );
}

export type { MastraInterrupt };
export { parseInterruptValue, AskUserPrompt, SubmitPlanPrompt };

const INTERRUPT_EVENT_NAME = "on_interrupt";

type PendingInterruptEvent = {
  name: string;
  value: unknown;
};

function isCollaborationInterruptEvent(event: PendingInterruptEvent): boolean {
  const interrupt = parseInterruptValue(event.value);
  return interrupt?.toolName === "ask_user" || interrupt?.toolName === "submit_plan";
}

/** CopilotKit interrupt UI for Mastra ask_user / submit_plan suspension. */
export function CollaborationInterruptHandler({
  agentId,
  threadId,
}: {
  agentId: string;
  threadId: string;
}) {
  const { copilotkit } = useCopilotKit();
  const { agent } = useAgent({ agentId });
  const { getRunForwardedProps } = useDataTaskChatInputBindings();
  const { liveRun } = useLiveRun();
  const collaborationResponses = useThreadCollaborationResponsesForChat(threadId);
  const [pendingEvent, setPendingEvent] = useState<PendingInterruptEvent | null>(null);
  const pendingEventRef = useRef(pendingEvent);
  pendingEventRef.current = pendingEvent;
  const submittingRef = useRef(false);

  useEffect(() => {
    let localInterrupt: PendingInterruptEvent | null = null;
    const finalizeInterrupt = () => {
      if (localInterrupt) {
        setPendingEvent(localInterrupt);
        localInterrupt = null;
      }
    };
    const subscription = agent.subscribe({
      onCustomEvent: ({ event }) => {
        if (event.name !== INTERRUPT_EVENT_NAME) {
          return;
        }
        const wrapped = { name: event.name, value: event.value };
        if (!isCollaborationInterruptEvent(wrapped)) {
          return;
        }
        localInterrupt = wrapped;
      },
      onRunStartedEvent: () => {
        localInterrupt = null;
        setPendingEvent(null);
      },
      onRunFinalized: finalizeInterrupt,
      onRunFinishedEvent: finalizeInterrupt,
      onRunFailed: () => {
        localInterrupt = null;
        setPendingEvent(null);
      },
    });
    return () => subscription.unsubscribe();
  }, [agent]);

  const interruptElement = useMemo(() => {
    if (!pendingEvent || agent.threadId !== threadId) {
      return null;
    }

    const interrupt = parseInterruptValue(pendingEvent.value);
    if (!interrupt?.toolName) {
      return null;
    }

    const canResume = canResumeCollaborationInterrupt({
      toolCallId: interrupt.toolCallId,
      collaborationResponses,
      liveRun,
      liveRunStatus: liveRun.runStatus,
    });
    const showPrompt =
      canResume ||
      liveRun.runStatus === "suspended" ||
      liveRun.runStatus === "running";
    if (!showPrompt) {
      return null;
    }

    const submitLive = (response: unknown) => {
      if (submittingRef.current || !canResume) {
        return;
      }
      submittingRef.current = true;
      clearPendingCollaborationInterrupt(threadId, "live");
      setPendingEvent(null);
      const forwardedProps = mergeRunForwardedPropsWithCommand(
        getRunForwardedProps(),
        {
          resume: response,
          interruptEvent: pendingEventRef.current?.value,
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
          onSubmit={submitLive}
          threadId={threadId}
          agentId={agentId}
          canResume={canResume}
        />
      );
    }

    return (
      <AskUserPrompt
        interrupt={interrupt}
        onSubmit={submitLive}
        threadId={threadId}
        agentId={agentId}
        canResume={canResume}
      />
    );
  }, [
    agent,
    agentId,
    collaborationResponses,
    copilotkit,
    getRunForwardedProps,
    liveRun,
    pendingEvent,
    threadId,
  ]);

  useEffect(() => {
    if (!interruptElement) {
      clearPendingCollaborationInterrupt(threadId, "live");
      return;
    }

    const pendingToolCall =
      findPendingCollaborationToolCall(liveRun, collaborationResponses, liveRun.runStatus);
    const interruptToolCallId = parseInterruptValue(pendingEvent?.value)?.toolCallId;
    const runningCollaborationCall = liveRun.toolCalls.find(
      (call) =>
        (call.name === "ask_user" || call.name === "submit_plan") && call.status === "running",
    );
    const toolCallId =
      pendingToolCall?.id ?? interruptToolCallId ?? runningCollaborationCall?.id;
    if (!toolCallId) {
      clearPendingCollaborationInterrupt(threadId, "live");
      return;
    }

    setPendingCollaborationInterrupt({
      threadId,
      toolCallId,
      element: interruptElement,
      source: "live",
    });

    return () => {
      clearPendingCollaborationInterrupt(threadId, "live");
    };
  }, [collaborationResponses, interruptElement, liveRun, pendingEvent, threadId]);

  return null;
}
