import { EventType } from "@ag-ui/client";
import { describe, expect, it } from "vitest";

import { generateStubEvents, resolveRuntimeStubScenario } from "./scenarios.js";
import { AGENT_INTERRUPT_TYPE, INTERRUPT_EVENT_NAME, V1_SYSTEM_PROMPT } from "./types.js";

const request = (content: string) => ({
  threadId: "session-1",
  runId: "run-1",
  messages: [{ id: "m1", role: "user" as const, content }],
  systemPrompt: V1_SYSTEM_PROMPT
});

describe("runtime stub scenarios", () => {
  it("selects interrupt, tool, and text from user phrasing", () => {
    expect(resolveRuntimeStubScenario(request("please interrupt now"))).toBe("interrupt");
    expect(resolveRuntimeStubScenario(request("make a plan"))).toBe("tool");
    expect(resolveRuntimeStubScenario(request("hello"))).toBe("text");
  });

  it("emits a standard interrupt payload", () => {
    const events = [...generateStubEvents(request("please interrupt"))];
    const interrupt = events.find((event) => event.type === EventType.CUSTOM && event.name === INTERRUPT_EVENT_NAME);
    expect(interrupt?.value).toMatchObject({
      type: AGENT_INTERRUPT_TYPE,
      toolName: "ask_user",
      runId: "run-1"
    });
  });
});
