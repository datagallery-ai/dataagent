import React from "react";
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { hydrateLiveRunFromConversation } from "../conversation-restore";
import {
  buildCollapsedStepSummary,
  buildStepToolSummaries,
} from "../step-tool-summary";
import { buildProcessToolGroups, deriveProcessGroupUsage } from "../process-tool-groups";
import { overviewSectionPlan } from "../task-console-layout";
import { ToolFormattedResult } from "../tool-result-format";
import {
  parseSchemaToolResult,
  parseSqlToolResult,
  parseToolResultRecord,
} from "../tool-result-normalize";
import {
  createInitialLiveRun,
  deriveRunUsage,
  reduceLiveRunEvent,
} from "../live-run-state";
import { buildTraceTimeline } from "../trace-timeline";
import {
  resolveToolDisplayStatus,
  resolveToolFailurePresentation,
  toolResultLooksLikeError,
} from "../tool-call-display";
import type { SessionConversationDto } from "../../../lib/config-api/types";
import {
  assistantMessagesWithParallelTools,
  assistantMessagesWithTools,
  buildLiveRunWithNestedSqlResult,
  buildLiveRunWithObservationWrappedSchema,
  buildLiveRunWithParallelTools,
  buildLiveRunWithToolSequence,
  toolResultFixtures,
} from "../tool-display-fixtures";

const renderTool = (toolName: string, result: unknown) =>
  renderToStaticMarkup(
    ToolFormattedResult({ toolName, result, variant: "console", showRawFallback: false }),
  );

describe("tool display regression pipeline", () => {
  it("derives SQL timeline metrics from nested AG-UI payloads", () => {
    const run = buildLiveRunWithNestedSqlResult();
    const event = run.events.find((entry) => entry.id === "tc-sql-nested");
    expect(event?.kind).toBe("query");
    expect(event?.payload).toMatchObject({
      scannedRows: 1,
      durationMs: 24,
    });
    expect(parseSqlToolResult(run.toolCalls[0]?.result)).toMatchObject({
      columns: ["total_orders"],
      row_count: 1,
    });
  });

  it("derives schema tables from observation-wrapped payloads", () => {
    const run = buildLiveRunWithObservationWrappedSchema();
    const event = run.events.find((entry) => entry.id === "tc-schema-wrap");
    expect(event?.kind).toBe("inspect");
    expect((event?.payload as { tables?: Array<{ name: string }> }).tables?.[0]?.name).toBe(
      "orders",
    );
    expect(parseSchemaToolResult(run.toolCalls[0]?.result)?.tables?.[0]?.name).toBe("orders");
  });

  it("builds trace entries with normalized SQL and schema payloads", () => {
    let run = buildLiveRunWithNestedSqlResult();
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "sql_audit",
      value: {
        audit_log_id: "audit-nested",
        datasource_id: "api-duckdb-demo",
        status: "success",
        row_count: 1,
        elapsed_ms: 24,
      },
    });

    const nestedEntry = buildTraceTimeline(run).find(
      (entry) => entry.toolCallId === "tc-sql-nested",
    );
    expect(nestedEntry?.scannedRows).toBe(1);
    expect(nestedEntry?.durationMs).toBe(24);

    const schemaRun = buildLiveRunWithObservationWrappedSchema();
    const schemaEntry = buildTraceTimeline(schemaRun).find(
      (entry) => entry.toolCallId === "tc-schema-wrap",
    );
    expect(schemaEntry?.schemaTables?.[0]?.name).toBe("orders");
  });

  it("hydrates restored tool calls into displayable timeline and trace entries", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-regression",
      messages: [],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "sql-restored",
          status: "completed",
          toolName: "run_sql_readonly",
          callEventSeq: 1,
          resultPreview: JSON.stringify(toolResultFixtures.run_sql_readonly.nested),
        },
        {
          runId: "run-1",
          toolCallId: "schema-restored",
          status: "completed",
          toolName: "inspect_schema",
          callEventSeq: 2,
          resultPreview: JSON.stringify({
            observation: JSON.stringify(toolResultFixtures.inspect_schema.direct),
          }),
        },
      ],
    };

    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
    run = hydrateLiveRunFromConversation(run, dto);

    const sqlEvent = run.events.find((entry) => entry.id === "sql-restored");
    expect(sqlEvent?.payload).toMatchObject({
      scannedRows: 1,
      durationMs: 24,
      sql: "SELECT COUNT(*) AS total_orders FROM orders",
    });

    const schemaEvent = run.events.find((entry) => entry.id === "schema-restored");
    expect((schemaEvent?.payload as { tables?: Array<{ name: string }> }).tables?.[0]?.name).toBe(
      "orders",
    );

    const trace = buildTraceTimeline(run);
    expect(trace.find((entry) => entry.toolCallId === "sql-restored")?.scannedRows).toBe(1);
    expect(
      trace.find((entry) => entry.toolCallId === "schema-restored")?.schemaTables?.[0]?.name,
    ).toBe("orders");
  });
});

describe("middle column regression helpers", () => {
  it("groups parallel tools under one process step", () => {
    const liveRun = buildLiveRunWithParallelTools();
    const groups = buildProcessToolGroups([...assistantMessagesWithParallelTools], liveRun);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.toolCallIds).toEqual(["tc-list", "tc-schema"]);
    expect(groups[0]?.title).toContain("parallel");
  });

  it("builds collapsed summaries and chip labels for active steps", () => {
    const liveRun = buildLiveRunWithToolSequence();
    const summaries = buildStepToolSummaries({
      toolCalls: [
        { id: "tc-list", type: "function", function: { name: "list_data_sources" } },
        { id: "tc-schema", type: "function", function: { name: "inspect_schema" } },
      ],
      liveRun,
      isActive: true,
    });
    const collapsed = buildCollapsedStepSummary({
      thinking: "I'll inspect the datasource first.",
      tools: summaries,
    });
    expect(collapsed.thinkingPreview).toContain("inspect the datasource");
    expect(collapsed.toolSummary).toContain("List data sources");
    expect(collapsed.toolSummary).toContain("Inspect");
  });

  it("maps backend and copilot statuses consistently for cards", () => {
    expect(
      resolveToolDisplayStatus({
        copilotStatus: "inProgress",
        backendPhase: "running",
        hasResult: false,
      }),
    ).toBe("executing");
    expect(
      resolveToolDisplayStatus({
        copilotStatus: "complete",
        backendPhase: "success",
        hasResult: true,
        resultIsError: toolResultLooksLikeError(JSON.stringify(toolResultFixtures.protocolError)),
      }),
    ).toBe("failed");
    expect(resolveToolFailurePresentation(JSON.stringify(toolResultFixtures.protocolError)).title).toBe(
      "Result sync failed",
    );
  });
});

describe("right panel regression helpers", () => {
  it("shows four overview blocks for a completed multi-tool run", () => {
    const liveRun = buildLiveRunWithToolSequence();
    const runUsage = deriveRunUsage(liveRun);
    const toolGroups = buildProcessToolGroups([...assistantMessagesWithTools], liveRun);
    const sections = overviewSectionPlan({
      hasToolDistribution: Object.keys(runUsage.toolCalls.byTool).length > 0,
    });

    expect(sections.map((section) => section.id)).toEqual([
      "conclusion",
      "progress",
      "tool-distribution",
    ]);
    expect(deriveProcessGroupUsage(toolGroups, liveRun).stepCount).toBe(2);
    expect(runUsage.toolCalls.success).toBe(3);
  });
});

describe("tool result formatting matrix", () => {
  const matrix: Array<{ tool: string; result: unknown; expectText: string }> = [
    { tool: "list_data_sources", result: toolResultFixtures.list_data_sources.direct, expectText: "API DuckDB Demo" },
    { tool: "inspect_schema", result: toolResultFixtures.inspect_schema.direct, expectText: "orders" },
    { tool: "preview_table", result: toolResultFixtures.preview_table.direct, expectText: "orders" },
    {
      tool: "preview_table",
      result: toolResultFixtures.preview_table_object_rows,
      expectText: "42.5",
    },
    { tool: "run_sql_readonly", result: toolResultFixtures.run_sql_readonly.flat, expectText: "128" },
    { tool: "run_sql_readonly", result: toolResultFixtures.run_sql_readonly.nested, expectText: "128" },
    {
      tool: "inspect_schema",
      result: toolResultFixtures.inspect_schema.observationWrapped,
      expectText: "orders",
    },
    {
      tool: "run_sql_readonly",
      result: toolResultFixtures.run_sql_readonly.observationWrapped,
      expectText: "128",
    },
    { tool: "retrieve_knowledge", result: toolResultFixtures.retrieve_knowledge.direct, expectText: "Refunds" },
    { tool: "write_file", result: toolResultFixtures.write_file, expectText: "reports/summary.md" },
    { tool: "read_file", result: toolResultFixtures.read_file, expectText: "Revenue increased" },
    { tool: "edit_file", result: toolResultFixtures.edit_file, expectText: "Replaced 1 occurrence" },
    { tool: "list_files", result: toolResultFixtures.list_files, expectText: "summary.md" },
    { tool: "grep", result: toolResultFixtures.grep, expectText: "total revenue" },
    { tool: "file_stat", result: toolResultFixtures.file_stat, expectText: "128 bytes" },
    { tool: "mkdir", result: toolResultFixtures.mkdir, expectText: "reports/archive" },
    { tool: "execute_command", result: toolResultFixtures.execute_command, expectText: "verify-ok" },
    { tool: "task_write", result: toolResultFixtures.task_write, expectText: "Inspect schema" },
    { tool: "ask_user", result: toolResultFixtures.ask_user, expectText: "Waiting for the user" },
    { tool: "submit_plan", result: toolResultFixtures.submit_plan, expectText: "Plan submitted" },
  ];

  it.each(matrix)("renders $tool with structured output", ({ tool, result, expectText }) => {
    expect(renderTool(tool, result)).toContain(expectText);
  });

  it("unwraps observation envelopes for every structured data tool", () => {
    expect(parseToolResultRecord(toolResultFixtures.list_data_sources.observationWrapped)).toMatchObject({
      datasources: [{ id: "api-duckdb-demo" }],
    });
    expect(parseToolResultRecord(toolResultFixtures.run_sql_readonly.observationWrapped)).toMatchObject({
      row_count: 1,
    });
  });
});
