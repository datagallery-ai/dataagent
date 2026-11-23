import { describe, expect, it } from "vitest";
import {
  buildProcessToolGroups,
  deriveProcessGroupUsage,
} from "../process-tool-groups";
import { createInitialLiveRun } from "../live-run-state";

describe("process tool groups", () => {
  it("groups parallel tool calls from one assistant message into one process step", () => {
    const liveRun = {
      ...createInitialLiveRun(),
      toolCalls: [
        {
          id: "tool-a",
          name: "list_data_sources",
          status: "success" as const,
          startedAtMs: 10,
          finishedAtMs: 30,
        },
        {
          id: "tool-b",
          name: "inspect_schema",
          status: "running" as const,
          startedAtMs: 12,
        },
      ],
    };
    const messages = [
      {
        id: "assistant-1",
        role: "assistant",
        toolCalls: [
          { id: "tool-a", function: { name: "list_data_sources" } },
          { id: "tool-b", function: { name: "inspect_schema" } },
        ],
      },
    ];

    const groups = buildProcessToolGroups(messages, liveRun);

    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({
      id: "group-assistant-1",
      messageId: "assistant-1",
      toolCallIds: ["tool-a", "tool-b"],
      status: "running",
      startedAtMs: 10,
      stepNumber: 1,
      title: "Run 2 tools in parallel",
    });
    expect(groups[0]?.summary).toContain("List data sources");
    expect(groups[0]?.summary).toContain("Inspect data source schema");
  });

  it("summarizes repeated parallel calls to the same tool with a count", () => {
    const liveRun = {
      ...createInitialLiveRun(),
      toolCalls: [
        { id: "tool-a", name: "list_data_sources", status: "success" as const },
        { id: "tool-b", name: "list_data_sources", status: "success" as const },
        { id: "tool-c", name: "list_data_sources", status: "success" as const },
      ],
    };
    const groups = buildProcessToolGroups(
      [
        {
          id: "assistant-1",
          role: "assistant",
          toolCalls: [
            { id: "tool-a", function: { name: "list_data_sources" } },
            { id: "tool-b", function: { name: "list_data_sources" } },
            { id: "tool-c", function: { name: "list_data_sources" } },
          ],
        },
      ],
      liveRun,
    );

    expect(groups[0]?.title).toBe("Run 3 tools in parallel");
    expect(groups[0]?.summary).toBe("List data sources × 3");
  });

  it("falls back to singleton groups for live tool calls without message ownership", () => {
    const liveRun = {
      ...createInitialLiveRun(),
      toolCalls: [
        {
          id: "restored-sql",
          name: "run_sql_readonly",
          status: "success" as const,
          startedAtMs: 20,
          finishedAtMs: 40,
        },
      ],
    };

    const groups = buildProcessToolGroups([], liveRun);

    expect(groups).toEqual([
      expect.objectContaining({
        id: "group-restored-sql",
        messageId: undefined,
        toolCallIds: ["restored-sql"],
        status: "success",
        stepNumber: 1,
        title: "Run SQL query",
      }),
    ]);
  });

  it("derives step and tool usage separately", () => {
    const liveRun = {
      ...createInitialLiveRun(),
      toolCalls: [
        { id: "tool-a", name: "list_data_sources", status: "success" as const },
        { id: "tool-b", name: "inspect_schema", status: "failed" as const },
      ],
    };
    const groups = buildProcessToolGroups(
      [
        {
          id: "assistant-1",
          role: "assistant",
          toolCalls: [
            { id: "tool-a", function: { name: "list_data_sources" } },
            { id: "tool-b", function: { name: "inspect_schema" } },
          ],
        },
      ],
      liveRun,
    );

    expect(deriveProcessGroupUsage(groups, liveRun)).toMatchObject({
      stepCount: 1,
      toolCallCount: 2,
      completedSteps: 0,
      failedSteps: 1,
      runningSteps: 0,
    });
  });
});
