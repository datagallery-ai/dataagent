import { describe, expect, it } from "vitest";
import { buildTraceTimeline } from "../trace-timeline";
import {
  createInitialLiveRun,
  reduceLiveRunEvent,
} from "../live-run-state";
import { hydrateLiveRunFromConversation } from "../conversation-restore";
import type { SessionConversationDto } from "../../../lib/config-api/types";

describe("buildTraceTimeline", () => {
  it("returns empty timeline for idle run with no activity", () => {
    expect(buildTraceTimeline(createInitialLiveRun())).toEqual([]);
  });

  it("merges tool calls, events, audits, and run boundaries", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-schema",
      toolCallName: "inspect_schema",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-schema",
      toolCallName: "inspect_schema",
      content: JSON.stringify({
        tables: [{ name: "orders", columns: [{ name: "id", type: "INT" }] }],
      }),
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-sql",
      toolCallName: "run_sql_readonly",
      args: { sql: "SELECT 1" },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "sql_audit",
      value: {
        audit_log_id: "audit-1",
        datasource_id: "api-duckdb-demo",
        status: "success",
        row_count: 42,
        elapsed_ms: 18,
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-sql",
      toolCallName: "run_sql_readonly",
      content: JSON.stringify({ row_count: 42, elapsed_ms: 18 }),
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: { id: "artifact-1", title: "查询结果", summary: "数据集" },
    });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });

    const entries = buildTraceTimeline(run);
    expect(entries[0]?.kind).toBe("run_started");
    expect(entries.at(-1)?.kind).toBe("run_finished");

    const schemaEntry = entries.find((entry) => entry.toolCallId === "tool-schema");
    expect(schemaEntry?.schemaTables?.[0]?.name).toBe("orders");

    const sqlEntry = entries.find((entry) => entry.toolCallId === "tool-sql");
    expect(sqlEntry?.sql).toBe("SELECT 1");
    expect(sqlEntry?.scannedRows).toBe(42);
    expect(sqlEntry?.auditStatus).toBe("success");
    expect(sqlEntry?.datasourceId).toBe("api-duckdb-demo");
    expect(sqlEntry?.artifactIds).toContain("artifact-1");
    expect(sqlEntry?.ts).toMatch(/^\d{2}:\d{2}:\d{2}$/);

    expect(entries.find((entry) => entry.kind === "artifact")).toBeUndefined();
  });

  it("shows standalone artifact entries with dataset preview detail", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-2",
        type: "table",
        name: "第二次查询结果",
        preview_json: {
          columns: ["metric"],
          rows: [["100"]],
          row_count: 1,
        },
      },
    });

    const entries = buildTraceTimeline(run);
    const artifactEntry = entries.find((entry) => entry.kind === "artifact");
    expect(artifactEntry?.title).toBe("第二次查询结果");
    expect(artifactEntry?.artifactDetail).toEqual({
      type: "dataset",
      columns: ["metric"],
      rows: [["100"]],
    });
    expect(artifactEntry?.ts).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });

  it("includes run failure entry with error message", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "RUN_ERROR",
      message: "连接超时",
    });

    const entries = buildTraceTimeline(run);
    expect(entries.at(-1)).toMatchObject({
      kind: "run_failed",
      errorMessage: "连接超时",
    });
  });

  it("shows failed badge when ACTIVITY STEP failed without tool call", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "ACTIVITY_SNAPSHOT",
      activityType: "STEP",
      messageId: "run-1:activity:step:sql-1",
      content: {
        step_id: "sql-1",
        title: "Run read-only SQL",
        tool_name: "run_sql_readonly",
        status: "failed",
        sql: "SELECT 1",
        error_message: "permission denied",
      },
    });

    const entry = buildTraceTimeline(run).find((item) => item.eventId === "sql-1");
    expect(entry).toMatchObject({
      summary: "Failed.",
      toolStatus: "failed",
    });
  });

  it("does not append orphan ACTIVITY STEP cards after a later run starts", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED", runId: "run-1" });
    // ACTIVITY arrives before TOOL_CALL (production ordering) and must be absorbed.
    run = reduceLiveRunEvent(run, {
      type: "ACTIVITY_SNAPSHOT",
      activityType: "STEP",
      content: {
        step_id: "schema-orphan",
        title: "Inspect data source schema",
        tool_name: "inspect_schema",
        status: "completed",
        content: {
          tables: [{ name: "orders", columns: [{ name: "id", type: "TEXT" }] }],
        },
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "call_inspect",
      toolCallName: "inspect_schema",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "call_inspect",
      toolCallName: "inspect_schema",
      result: JSON.stringify({
        tables: [{ name: "orders", columns: [{ name: "id", type: "TEXT" }] }],
      }),
    });
    run = reduceLiveRunEvent(run, {
      type: "ACTIVITY_SNAPSHOT",
      activityType: "STEP",
      content: {
        step_id: "sql-1",
        title: "Run read-only SQL",
        tool_name: "run_sql_readonly",
        status: "completed",
        sql: "WITH monthly_sales AS (\n  SELECT 1\n)\nSELECT * FROM monthly_sales",
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "call_sql",
      toolCallName: "run_sql_readonly",
      args: { sql: "WITH monthly_sales AS (\n  SELECT 1\n)\nSELECT * FROM monthly_sales" },
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "call_sql",
      toolCallName: "run_sql_readonly",
      result: JSON.stringify({ row_count: 1, elapsed_ms: 5 }),
    });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED", runId: "run-2" });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });

    const entries = buildTraceTimeline(run);
    const toolEntries = entries.filter((entry) => entry.kind === "tool");
    expect(toolEntries).toHaveLength(2);
    expect(toolEntries.map((entry) => entry.toolCallId).sort()).toEqual([
      "call_inspect",
      "call_sql",
    ]);

    const lastToolIndex = Math.max(
      ...entries
        .map((entry, index) => (entry.kind === "tool" ? index : -1))
        .filter((index) => index >= 0),
    );
    const secondRunStart = entries.findIndex((entry) => entry.id === "run-started-current");
    expect(secondRunStart).toBeGreaterThan(lastToolIndex);
    expect(entries.some((entry) => entry.id === "event-schema-orphan")).toBe(false);
    expect(entries.some((entry) => entry.id === "event-sql-1")).toBe(false);
  });

  it("shows run suspended instead of finished after ask_user suspension", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "call-ask-1",
    });
    run = reduceLiveRunEvent(run, {
      type: "STATE_DELTA",
      delta: [{ op: "replace", path: "/runStatus", value: "suspended" }],
    });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });

    const entries = buildTraceTimeline(run);
    expect(entries.some((entry) => entry.kind === "run_finished")).toBe(false);
    expect(entries.at(-1)).toMatchObject({
      kind: "run_suspended",
      title: "Run paused",
      summary: "Agent is waiting for your response before continuing.",
    });
  });

  it("shows run resumed after a suspended segment is archived", () => {
    const run = {
      ...createInitialLiveRun(),
      runStatus: "running" as const,
      runStartedAt: 2_000,
      runHistory: [
        {
          startedAt: 1_000,
          finishedAt: 1_500,
          status: "suspended" as const,
          toolCallEndIndex: 1,
          auditEndIndex: 0,
        },
      ],
      toolCalls: [
        {
          id: "call-ask-1",
          name: "ask_user",
          status: "success" as const,
        },
      ],
    };

    const entries = buildTraceTimeline(run);
    expect(entries.find((entry) => entry.id === "run-history-0-suspended")).toMatchObject({
      title: "Run paused",
    });
    expect(entries.find((entry) => entry.id === "run-started-current")).toMatchObject({
      title: "Run resumed",
      summary: "Agent received your response and resumed.",
    });
  });

  it("shows restored tool calls instead of replay-only run boundaries", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "schema-1",
          status: "completed",
          toolName: "inspect_schema",
          callEventSeq: 1,
          resultPreview: JSON.stringify({
            tables: [{ name: "orders", columns: [{ name: "id", type: "INT" }] }],
          }),
        },
        {
          runId: "run-1",
          toolCallId: "sql-1",
          status: "completed",
          toolName: "run_sql_readonly",
          callEventSeq: 2,
          resultPreview: JSON.stringify({ row_count: 12, elapsed_ms: 8 }),
        },
      ],
    };

    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: { id: "artifact-orphan", title: "旧产出", summary: "" },
    });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });

    const corruptedEntries = buildTraceTimeline(run);
    expect(corruptedEntries.every((entry) => entry.kind !== "tool")).toBe(true);
    expect(corruptedEntries.filter((entry) => entry.kind === "run_started")).toHaveLength(2);

    run = hydrateLiveRunFromConversation(run, dto);
    const entries = buildTraceTimeline(run);

    expect(entries.filter((entry) => entry.kind === "tool")).toHaveLength(2);
    expect(entries.filter((entry) => entry.kind === "run_started")).toHaveLength(1);
    expect(entries.filter((entry) => entry.kind === "run_finished")).toHaveLength(1);
    expect(entries.find((entry) => entry.toolCallId === "schema-1")?.title).toBe(
      "Inspect data source schema",
    );
  });

  it("normalizes nested SQL and observation-wrapped schema tool results", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-sql-nested",
      toolCallName: "run_sql_readonly",
      args: { sql: "SELECT COUNT(*) AS total_orders FROM orders" },
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-sql-nested",
      toolCallName: "run_sql_readonly",
      content: JSON.stringify({
        sql: "SELECT COUNT(*) AS total_orders FROM orders",
        result: {
          columns: ["total_orders"],
          rows: [[128]],
          row_count: 128,
          elapsed_ms: 33,
        },
      }),
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-schema-wrap",
      toolCallName: "inspect_schema",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-schema-wrap",
      toolCallName: "inspect_schema",
      content: JSON.stringify({
        observation: JSON.stringify({
          tables: [{ name: "orders", columns: [{ name: "id", type: "INT" }] }],
        }),
      }),
    });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });

    const entries = buildTraceTimeline(run);
    expect(entries.find((entry) => entry.toolCallId === "tool-sql-nested")?.scannedRows).toBe(128);
    expect(
      entries.find((entry) => entry.toolCallId === "tool-schema-wrap")?.schemaTables?.[0]?.name,
    ).toBe("orders");
  });

  it("interleaves tool calls between archived run segments", () => {
    const run = {
      ...createInitialLiveRun(),
      runStatus: "completed" as const,
      runStartedAt: 3_000,
      runFinishedAt: 4_000,
      runHistory: [
        {
          startedAt: 1_000,
          finishedAt: 1_500,
          status: "suspended" as const,
          toolCallEndIndex: 1,
          auditEndIndex: 0,
        },
        {
          startedAt: 2_000,
          finishedAt: 2_500,
          status: "completed" as const,
          toolCallEndIndex: 2,
          auditEndIndex: 0,
        },
      ],
      toolCalls: [
        { id: "tc-ask", name: "ask_user", status: "success" as const },
        { id: "tc-resume", name: "list_data_sources", status: "success" as const },
        { id: "tc-sql", name: "run_sql_readonly", status: "success" as const },
      ],
    };

    const entries = buildTraceTimeline(run);
    const entryIndex = (id: string) => entries.findIndex((entry) => entry.id === id);

    expect(entryIndex("run-history-0-started")).toBeGreaterThanOrEqual(0);
    expect(entryIndex("run-history-0-suspended")).toBeGreaterThan(entryIndex("run-history-0-started"));
    expect(entryIndex("tool-tc-ask")).toBeGreaterThan(entryIndex("run-history-0-started"));
    expect(entryIndex("tool-tc-ask")).toBeLessThan(entryIndex("run-history-0-suspended"));

    expect(entryIndex("run-history-1-started")).toBeGreaterThan(entryIndex("run-history-0-suspended"));
    expect(entryIndex("tool-tc-resume")).toBeGreaterThan(entryIndex("run-history-1-started"));
    expect(entryIndex("tool-tc-resume")).toBeLessThan(entryIndex("run-history-1-finished"));

    expect(entryIndex("run-started-current")).toBeGreaterThan(entryIndex("run-history-1-finished"));
    expect(entryIndex("tool-tc-sql")).toBeGreaterThan(entryIndex("run-started-current"));
    expect(entryIndex("run-finished-current")).toBeGreaterThan(entryIndex("tool-tc-sql"));
  });
});
