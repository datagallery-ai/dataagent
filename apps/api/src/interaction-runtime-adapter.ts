import type { BaseEvent, RunAgentInput } from "@ag-ui/client";
import { EventType } from "@ag-ui/core";
import { createCustomEvent } from "@datafoundry/agent-runtime";
import type { MetadataStore } from "@datafoundry/metadata";
import { createHash, randomUUID } from "node:crypto";

export type InteractionInterrupt = {
  args: unknown;
  resumeSchema: unknown;
  runId: string;
  suspendPayload: unknown;
  toolCallId: string;
  toolName: "ask_user" | "submit_plan";
};

export type InteractionResume = {
  fingerprint: string;
  interrupt: InteractionInterrupt;
  response: unknown;
};

export class InteractionRuntimeAdapter {
  constructor(
    private readonly metadataStore: MetadataStore,
    private readonly userId: string,
    private readonly sessionId: string,
    private readonly runId: string
  ) {}

  /**
   * Convert Mastra's interrupt event into the stable application interaction event.
   * Returns both the parsed interrupt (for TOOL_CALL_START persistence) and the
   * `interaction.requested` CUSTOM event.
   */
  capture(event: BaseEvent): { interrupt: InteractionInterrupt; event: BaseEvent } | undefined {
    const interrupt = parseInterruptEvent(event);
    if (!interrupt) {
      return undefined;
    }
    validateInterruptIdentity(interrupt, this.runId);
    const interaction = this.metadataStore.interactions.request({
      id: randomUUID(),
      user_id: this.userId,
      session_id: this.sessionId,
      run_id: this.runId,
      tool_call_id: interrupt.toolCallId,
      tool_name: interrupt.toolName,
      payload: interrupt.suspendPayload,
      interrupt_event: readInterruptEventValue(event, interrupt)
    });
    return {
      interrupt,
      event: createCustomEvent("interaction.requested", {
        interaction_id: interaction.id,
        interrupt_event: JSON.stringify(interrupt),
        payload: interrupt.suspendPayload,
        resume_schema: interrupt.resumeSchema,
        run_id: this.runId,
        tool_call_id: interrupt.toolCallId,
        tool_name: interrupt.toolName
      })
    };
  }

  /** Validate and persist one idempotent resume response. */
  resolve(resume: InteractionResume): BaseEvent {
    validateInterruptIdentity(resume.interrupt, this.runId);
    const interaction = this.metadataStore.interactions.getByToolCall({
      user_id: this.userId,
      run_id: this.runId,
      tool_call_id: resume.interrupt.toolCallId
    });
    if (interaction.session_id !== this.sessionId || interaction.tool_name !== resume.interrupt.toolName) {
      throw new Error(`INTERACTION_IDENTITY_MISMATCH:${resume.interrupt.toolCallId}`);
    }
    const resolved = this.metadataStore.interactions.resolve({
      user_id: this.userId,
      run_id: this.runId,
      tool_call_id: resume.interrupt.toolCallId,
      resume_fingerprint: resume.fingerprint,
      response: resume.response
    });
    return createCustomEvent("interaction.resolved", {
      interaction_id: resolved.id,
      response: resume.response,
      run_id: this.runId,
      tool_call_id: resume.interrupt.toolCallId,
      tool_name: resume.interrupt.toolName
    });
  }

  /** Cancel one pending interaction using CopilotKit's resume=false command. */
  cancel(resume: InteractionResume): BaseEvent {
    validateInterruptIdentity(resume.interrupt, this.runId);
    const canceled = this.metadataStore.interactions.cancel({
      user_id: this.userId,
      run_id: this.runId,
      tool_call_id: resume.interrupt.toolCallId,
      resume_fingerprint: resume.fingerprint
    });
    return createCustomEvent("interaction.resolved", {
      interaction_id: canceled.id,
      run_id: this.runId,
      status: "canceled",
      tool_call_id: resume.interrupt.toolCallId,
      tool_name: resume.interrupt.toolName
    });
  }
}

/**
 * Build a TOOL_CALL_START event for a HITL interrupt so suspend persists the tool
 * call alongside the interactions row (R-018 atomic HITL contract).
 */
export const buildHitlToolCallStartEvent = (interrupt: InteractionInterrupt): BaseEvent =>
  ({
    type: EventType.TOOL_CALL_START,
    toolCallId: interrupt.toolCallId,
    toolCallName: interrupt.toolName,
    ...(interrupt.args !== undefined ? { args: interrupt.args } : {}),
    timestamp: Date.now()
  }) as BaseEvent;

/**
 * Pair with {@link buildHitlToolCallStartEvent} before the transport-only RUN_FINISHED.
 * Mastra's on_interrupt path never emits TOOL_CALL_END; without it, AbstractAgent
 * verifyEvents rejects RUN_FINISHED while the tool call is still active.
 */
export const buildHitlToolCallEndEvent = (interrupt: InteractionInterrupt): BaseEvent =>
  ({
    type: EventType.TOOL_CALL_END,
    toolCallId: interrupt.toolCallId,
    toolCallName: interrupt.toolName,
    timestamp: Date.now()
  }) as BaseEvent;

export type HitlToolCallBoundaryState = {
  endedToolCallIds: Set<string>;
  startedToolCallIds: Set<string>;
};

/**
 * Server event-bridge for HITL suspend (persisted stream events only).
 * Ensures START (if missing) and END (if missing) around the interaction /
 * on_interrupt events. Caller must still send a transport-only RUN_FINISHED
 * via the subscriber (not through the persist pipeline).
 */
export function buildHitlSuspendBridgeEvents(input: {
  interrupt: InteractionInterrupt;
  interactionEvent: BaseEvent;
  passthroughInterruptEvent?: BaseEvent;
  state: HitlToolCallBoundaryState;
}): BaseEvent[] {
  const toolCallId = input.interrupt.toolCallId;
  const events: BaseEvent[] = [];

  if (!input.state.startedToolCallIds.has(toolCallId)) {
    events.push(buildHitlToolCallStartEvent(input.interrupt));
    input.state.startedToolCallIds.add(toolCallId);
  }

  events.push(input.interactionEvent);
  if (input.passthroughInterruptEvent) {
    events.push(input.passthroughInterruptEvent);
  }

  if (!input.state.endedToolCallIds.has(toolCallId)) {
    events.push(buildHitlToolCallEndEvent(input.interrupt));
    input.state.endedToolCallIds.add(toolCallId);
  }

  return events;
}

/** Extract a Mastra resume command from an AG-UI run request. */
export const extractInteractionResume = (input: RunAgentInput): InteractionResume | undefined => {
  if (!isRecord(input.forwardedProps) || !isRecord(input.forwardedProps.command)) {
    return undefined;
  }
  const command = input.forwardedProps.command;
  if (!("resume" in command) || command.interruptEvent === undefined) {
    return undefined;
  }
  const interrupt = parseInterruptValue(command.interruptEvent);
  const response = command.resume;
  const fingerprint = createHash("sha256")
    .update(JSON.stringify({ interrupt, response }))
    .digest("hex");
  return { fingerprint, interrupt, response };
};

const parseInterruptEvent = (event: BaseEvent): InteractionInterrupt | undefined => {
  if (event.type !== EventType.CUSTOM || event.name !== "on_interrupt") {
    return undefined;
  }
  return parseInterruptValue(event.value);
};

const readInterruptEventValue = (
  event: BaseEvent,
  interrupt: InteractionInterrupt
): unknown => {
  const customEvent = event as BaseEvent & { value?: unknown };
  if (customEvent.value !== undefined) {
    return customEvent.value;
  }
  return {
    type: "mastra_suspend",
    args: interrupt.args,
    resumeSchema: interrupt.resumeSchema,
    runId: interrupt.runId,
    suspendPayload: interrupt.suspendPayload,
    toolCallId: interrupt.toolCallId,
    toolName: interrupt.toolName
  };
};

const parseInterruptValue = (value: unknown): InteractionInterrupt => {
  const parsed = typeof value === "string" ? parseJson(value) : value;
  if (
    !isRecord(parsed)
    || parsed.type !== "mastra_suspend"
    || typeof parsed.toolCallId !== "string"
    || typeof parsed.toolName !== "string"
    || typeof parsed.runId !== "string"
  ) {
    throw new Error("INVALID_INTERACTION_INTERRUPT");
  }
  if (parsed.toolName !== "ask_user" && parsed.toolName !== "submit_plan") {
    throw new Error(`UNSUPPORTED_INTERACTION_TOOL:${parsed.toolName}`);
  }
  return {
    args: parsed.args,
    resumeSchema: parsed.resumeSchema,
    runId: parsed.runId,
    suspendPayload: parsed.suspendPayload,
    toolCallId: parsed.toolCallId,
    toolName: parsed.toolName
  };
};

const validateInterruptIdentity = (interrupt: InteractionInterrupt, runId: string): void => {
  if (interrupt.runId !== runId) {
    throw new Error(`INTERACTION_RUN_MISMATCH:${interrupt.runId}`);
  }
};

const parseJson = (value: string): unknown => {
  try {
    return JSON.parse(value) as unknown;
  } catch {
    throw new Error("INVALID_INTERACTION_INTERRUPT_JSON");
  }
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;
