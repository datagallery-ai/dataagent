import { EventType, type BaseEvent } from "@ag-ui/client";
import { describe, expect, it } from "vitest";

import {
  buildHitlSuspendBridgeEvents,
  type HitlToolCallBoundaryState,
  type InteractionInterrupt
} from "./interaction-runtime-adapter.js";

const interrupt: InteractionInterrupt = {
  args: { question: "继续吗？" },
  resumeSchema: { type: "string" },
  runId: "run-1",
  suspendPayload: { question: "继续吗？" },
  toolCallId: "call_hitl_1",
  toolName: "ask_user"
};

const interactionEvent = {
  type: EventType.CUSTOM,
  name: "interaction.requested",
  value: { tool_call_id: interrupt.toolCallId },
  timestamp: 1
} as BaseEvent;

const onInterruptEvent = {
  type: EventType.CUSTOM,
  name: "on_interrupt",
  value: JSON.stringify(interrupt),
  timestamp: 2
} as BaseEvent;

function emptyState(): HitlToolCallBoundaryState {
  return {
    startedToolCallIds: new Set<string>(),
    endedToolCallIds: new Set<string>()
  };
}

describe("buildHitlSuspendBridgeEvents", () => {
  it("synthesizes START then END when upstream never started the tool call", () => {
    const state = emptyState();
    const events = buildHitlSuspendBridgeEvents({
      interrupt,
      interactionEvent,
      passthroughInterruptEvent: onInterruptEvent,
      state
    });

    expect(events.map((event) => event.type)).toEqual([
      EventType.TOOL_CALL_START,
      EventType.CUSTOM,
      EventType.CUSTOM,
      EventType.TOOL_CALL_END
    ]);
    expect(events[0]).toMatchObject({
      type: EventType.TOOL_CALL_START,
      toolCallId: interrupt.toolCallId,
      toolCallName: "ask_user"
    });
    expect(events[1]).toBe(interactionEvent);
    expect(events[2]).toBe(onInterruptEvent);
    expect(events[3]).toMatchObject({
      type: EventType.TOOL_CALL_END,
      toolCallId: interrupt.toolCallId
    });
    expect(state.startedToolCallIds.has(interrupt.toolCallId)).toBe(true);
    expect(state.endedToolCallIds.has(interrupt.toolCallId)).toBe(true);
  });

  it("still emits END before transport RUN_FINISHED when START already arrived", () => {
    const state = emptyState();
    state.startedToolCallIds.add(interrupt.toolCallId);

    const events = buildHitlSuspendBridgeEvents({
      interrupt,
      interactionEvent,
      passthroughInterruptEvent: onInterruptEvent,
      state
    });

    expect(events.map((event) => event.type)).toEqual([
      EventType.CUSTOM,
      EventType.CUSTOM,
      EventType.TOOL_CALL_END
    ]);
    expect(events.at(-1)).toMatchObject({
      type: EventType.TOOL_CALL_END,
      toolCallId: interrupt.toolCallId
    });
    // Caller appends transport-only RUN_FINISHED after this bridge sequence.
    expect(events.some((event) => event.type === EventType.RUN_FINISHED)).toBe(false);
    expect(state.endedToolCallIds.has(interrupt.toolCallId)).toBe(true);
  });

  it("does not duplicate END when the tool call already ended", () => {
    const state = emptyState();
    state.startedToolCallIds.add(interrupt.toolCallId);
    state.endedToolCallIds.add(interrupt.toolCallId);

    const events = buildHitlSuspendBridgeEvents({
      interrupt,
      interactionEvent,
      state
    });

    expect(events.map((event) => event.type)).toEqual([EventType.CUSTOM]);
    expect(events[0]).toBe(interactionEvent);
  });
});
