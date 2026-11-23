import { describe, expect, it } from "vitest";
import {
  createInitialLiveRun,
  reduceLiveRunEvent,
  type LiveRun,
} from "../live-run-state";
import { buildProcessToolGroups, deriveProcessGroupUsage } from "../process-tool-groups";
import { buildStepToolSummaries } from "../step-tool-summary";

type AgUiEvent = { type?: string; [key: string]: unknown };

function reduceAllEvents(events: AgUiEvent[]): LiveRun {
  return events.reduce((state, event) => reduceLiveRunEvent(state, event), createInitialLiveRun());
}

function assertNoRunningAfterCompletion(liveRun: LiveRun, messages: Array<Record<string, unknown>>) {
  expect(liveRun.runStatus).toBe("completed");
  expect(liveRun.toolCalls.every((call) => call.status !== "running")).toBe(true);

  const groups = buildProcessToolGroups(messages, liveRun);
  expect(deriveProcessGroupUsage(groups, liveRun).runningSteps).toBe(0);
  expect(groups.every((group) => group.status !== "running")).toBe(true);

  for (const message of messages) {
    if (message.role !== "assistant" || !Array.isArray(message.toolCalls)) continue;
    const summaries = buildStepToolSummaries({
      toolCalls: message.toolCalls as Array<{ id?: string; function?: { name?: string } }>,
      liveRun,
      isActive: false,
    });
    expect(summaries.every((tool) => tool.status !== "running")).toBe(true);
    expect(summaries.every((tool) => tool.durationLabel !== "Running")).toBe(true);
  }
}

describe("completed run display regression", () => {
  it("STATE_DELTA completed before RUN_FINISHED leaves zero running groups/tools", () => {
    const events: AgUiEvent[] = [
      { type: "RUN_STARTED" },
      {
        type: "TOOL_CALL_START",
        toolCallId: "tc-schema",
        toolCallName: "inspect_schema",
        parentMessageId: "msg-step-1",
      },
      {
        type: "TOOL_CALL_END",
        toolCallId: "tc-schema",
        toolCallName: "inspect_schema",
      },
      {
        type: "TOOL_CALL_START",
        toolCallId: "tc-sql-1",
        toolCallName: "run_sql_readonly",
        parentMessageId: "msg-step-2",
      },
      {
        type: "TOOL_CALL_RESULT",
        toolCallId: "tc-sql-1",
        toolCallName: "run_sql_readonly",
        result: JSON.stringify({ row_count: 2 }),
      },
      {
        type: "STATE_DELTA",
        delta: [{ op: "replace", path: "/runStatus", value: "completed" }],
      },
      { type: "RUN_FINISHED" },
    ];

    const messages = [
      {
        id: "msg-step-1",
        role: "assistant",
        toolCalls: [{ id: "tc-schema", type: "function", function: { name: "inspect_schema" } }],
      },
      {
        id: "msg-step-2",
        role: "assistant",
        toolCalls: [{ id: "tc-sql-1", type: "function", function: { name: "run_sql_readonly" } }],
      },
      { id: "msg-answer", role: "assistant", content: "GMV 对比报告已生成。" },
    ];

    const liveRun = reduceAllEvents(events);
    assertNoRunningAfterCompletion(liveRun, messages);
  });

  it("replays out-of-order RESULT-before-START tool events without residual running", () => {
    const events: AgUiEvent[] = [
      { type: "RUN_STARTED" },
      {
        type: "TOOL_CALL_RESULT",
        toolCallId: "tc-schema",
        toolCallName: "inspect_schema",
        result: JSON.stringify({ tables: [{ name: "orders", columns: [] }] }),
      },
      {
        type: "TOOL_CALL_START",
        toolCallId: "tc-schema",
        toolCallName: "inspect_schema",
        parentMessageId: "msg-step-1",
      },
      {
        type: "TOOL_CALL_END",
        toolCallId: "tc-schema",
        toolCallName: "inspect_schema",
      },
      {
        type: "STATE_DELTA",
        delta: [{ op: "replace", path: "/runStatus", value: "completed" }],
      },
      { type: "RUN_FINISHED" },
    ];

    const messages = [
      {
        id: "msg-step-1",
        role: "assistant",
        toolCalls: [{ id: "tc-schema", type: "function", function: { name: "inspect_schema" } }],
      },
    ];

    const liveRun = reduceAllEvents(events);
    assertNoRunningAfterCompletion(liveRun, messages);
  });

  it("replays multi-step parallel tool events without residual running", () => {
    const events: AgUiEvent[] = [
      { type: "RUN_STARTED" },
      {
        type: "TOOL_CALL_START",
        toolCallId: "tc-skill",
        toolCallName: "skill",
        parentMessageId: "msg-1",
      },
      {
        type: "TOOL_CALL_RESULT",
        toolCallId: "tc-skill",
        toolCallName: "skill",
        result: JSON.stringify({ loaded: true }),
      },
      {
        type: "TOOL_CALL_START",
        toolCallId: "tc-schema",
        toolCallName: "inspect_schema",
        parentMessageId: "msg-2",
      },
      {
        type: "TOOL_CALL_END",
        toolCallId: "tc-schema",
        toolCallName: "inspect_schema",
      },
      {
        type: "TOOL_CALL_START",
        toolCallId: "tc-sql-a",
        toolCallName: "run_sql_readonly",
        parentMessageId: "msg-3",
      },
      {
        type: "TOOL_CALL_START",
        toolCallId: "tc-sql-b",
        toolCallName: "run_sql_readonly",
        parentMessageId: "msg-3",
      },
      {
        type: "TOOL_CALL_RESULT",
        toolCallId: "tc-sql-a",
        toolCallName: "run_sql_readonly",
        result: JSON.stringify({ row_count: 1 }),
      },
      {
        type: "TOOL_CALL_RESULT",
        toolCallId: "tc-sql-b",
        toolCallName: "run_sql_readonly",
        result: JSON.stringify({ row_count: 1 }),
      },
      {
        type: "TOOL_CALL_START",
        toolCallId: "tc-publish",
        toolCallName: "publish_artifact",
        parentMessageId: "msg-4",
      },
      {
        type: "TOOL_CALL_END",
        toolCallId: "tc-publish",
        toolCallName: "publish_artifact",
      },
      {
        type: "STATE_DELTA",
        delta: [{ op: "replace", path: "/runStatus", value: "completed" }],
      },
      { type: "RUN_FINISHED" },
    ];

    const messages = [
      {
        id: "msg-1",
        role: "assistant",
        toolCalls: [{ id: "tc-skill", type: "function", function: { name: "skill" } }],
      },
      {
        id: "msg-2",
        role: "assistant",
        toolCalls: [{ id: "tc-schema", type: "function", function: { name: "inspect_schema" } }],
      },
      {
        id: "msg-3",
        role: "assistant",
        toolCalls: [
          { id: "tc-sql-a", type: "function", function: { name: "run_sql_readonly" } },
          { id: "tc-sql-b", type: "function", function: { name: "run_sql_readonly" } },
        ],
      },
      {
        id: "msg-4",
        role: "assistant",
        toolCalls: [{ id: "tc-publish", type: "function", function: { name: "publish_artifact" } }],
      },
      { id: "msg-answer", role: "assistant", content: "报告如下。" },
    ];

    const liveRun = reduceAllEvents(events);
    assertNoRunningAfterCompletion(liveRun, messages);
  });
});
