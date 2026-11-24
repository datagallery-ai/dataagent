/** @vitest-environment happy-dom */
import React, { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  LiveRunEventSubscriber,
  LiveRunProvider,
  useLiveRun,
} from "../use-data-gallery-run";
import { setHitlPending } from "../../../lib/hitl-state";

const mocks = vi.hoisted(() => ({
  agent: {
    state: {} as Record<string, unknown>,
    messages: [],
    subscribe: vi.fn(),
  },
}));
vi.mock("@copilotkit/react-core/v2", () => ({
  useAgent: () => ({ agent: mocks.agent }),
  useCopilotChatConfiguration: () => ({ threadId: "thread" }),
  UseAgentUpdate: {},
}));
vi.mock("../components/chat/collaboration-responses", () => ({
  getCollaborationResponsesForThread: () => [],
}));

let root: Root;
let host: HTMLDivElement;
function Snapshot() {
  const { liveRun } = useLiveRun("thread");
  return createElement("output", null, JSON.stringify({
    status: liveRun.runStatus,
    tools: liveRun.toolCalls.map(({ name, status }) => ({ name, status })),
  }));
}
function render() {
  root.render(createElement(LiveRunProvider, null,
    createElement(LiveRunEventSubscriber, { agentId: "dataagent", threadId: "thread" }),
    createElement(Snapshot),
  ));
}
function snapshot() {
  return JSON.parse(host.querySelector("output")!.textContent!);
}
async function emit(event: Record<string, unknown>) {
  const subscriber = mocks.agent.subscribe.mock.calls.at(-1)![0];
  await act(async () => subscriber.onEvent({ event }));
}

beforeEach(async () => {
  vi.stubGlobal("React", React);
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  mocks.agent.state = {};
  mocks.agent.subscribe.mockReturnValue({ unsubscribe: vi.fn() });
  setHitlPending("thread", []);
  window.sessionStorage.clear();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  await act(async () => render());
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  setHitlPending("thread", []);
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = false;
  vi.unstubAllGlobals();
});

it.each([
  ["request_human_feedback", false],
  ["execute", false],
  ["read_ir", true],
] as const)("preserves native waiting state with %s through event and state reconciliation", async (name, completed) => {
  await emit({ type: "RUN_STARTED", runId: "run-1" });
  await emit({ type: "TOOL_CALL_START", toolCallId: "tool-1", toolCallName: name });
  await emit({ type: "TOOL_CALL_END", toolCallId: "tool-1" });
  if (completed) {
    await emit({ type: "TOOL_CALL_RESULT", toolCallId: "tool-1", content: "done" });
  }
  // The SSE human_feedback event / REST restore supplies this independently of tools.
  setHitlPending("thread", [{ type: "human_feedback", interruptId: "ask", message: "Continue?" }]);
  await emit({ type: "RUN_FINISHED", metadata: { status: "waiting_for_input" } });
  const expected = { status: "suspended", tools: [{ name, status: completed ? "success" : "running" }] };
  expect(snapshot()).toEqual(expected);

  await emit({ type: "STATE_SNAPSHOT", snapshot: {} });
  expect(snapshot()).toEqual(expected);
  mocks.agent.state = { restored: true };
  await act(async () => render());
  expect(snapshot()).toEqual(expected);

  await emit({ type: "RUN_STARTED", runId: "answer-1" });
  expect(snapshot().status).toBe("running");
  setHitlPending("thread", []);
  await emit({ type: "RUN_FINISHED", metadata: { status: "completed" } });
  expect(snapshot().status).toBe("completed");
});

it("does not use another thread's pending interactions to suspend a completed run", async () => {
  setHitlPending("other", [{ type: "human_feedback", interruptId: "other-ask", message: "Continue?" }]);
  try {
    await emit({ type: "RUN_STARTED", runId: "run-1" });
    await emit({ type: "RUN_FINISHED", metadata: { status: "completed" } });
    expect(snapshot().status).toBe("completed");
  } finally {
    setHitlPending("other", []);
  }
});
