import React from "react";
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { buildProcessToolGroups, deriveProcessGroupUsage } from "../process-tool-groups";
import { overviewSectionPlan } from "../task-console-layout";
import { renderFormattedToolParams, ToolFormattedResult } from "../tool-result-format";
import {
  parseSchemaToolResult,
  parseSqlToolResult,
  parseToolResultRecord,
  toolResultObservationText,
  unwrapToolResultPayload,
} from "../tool-result-normalize";
import { deriveRunUsage } from "../live-run-state";
import {
  assistantMessagesWithTools,
  buildLiveRunWithToolSequence,
  toolParametersFixtures,
  toolResultFixtures,
} from "../tool-display-fixtures";

describe("unwrapToolResultPayload", () => {
  it("unwraps observation envelopes with JSON payloads", () => {
    expect(unwrapToolResultPayload(toolResultFixtures.list_data_sources.observationWrapped)).toEqual(
      toolResultFixtures.list_data_sources.direct,
    );
  });

  it("unwraps nested SQL execution payloads", () => {
    expect(unwrapToolResultPayload(toolResultFixtures.run_sql_readonly.nested)).toMatchObject(
      toolResultFixtures.run_sql_readonly.flat,
    );
  });

  it("preserves plain workspace observations", () => {
    expect(unwrapToolResultPayload(toolResultFixtures.write_file)).toBe(
      "Wrote 128 bytes to reports/summary.md",
    );
  });
});

describe("parseSqlToolResult", () => {
  it("accepts flat, nested, and observation-wrapped SQL payloads", () => {
    expect(parseSqlToolResult(JSON.stringify(toolResultFixtures.run_sql_readonly.flat))).toMatchObject({
      columns: ["total_orders"],
      row_count: 1,
    });
    expect(parseSqlToolResult(JSON.stringify(toolResultFixtures.run_sql_readonly.nested))).toMatchObject({
      columns: ["total_orders"],
      row_count: 1,
    });
    expect(parseSqlToolResult(JSON.stringify(toolResultFixtures.run_sql_readonly.observationWrapped))).toMatchObject({
      columns: ["total_orders"],
      row_count: 1,
    });
  });
});

describe("parseSchemaToolResult", () => {
  it("keeps schema tables for inspect_schema cards", () => {
    expect(parseSchemaToolResult(JSON.stringify(toolResultFixtures.inspect_schema.direct))).toMatchObject({
      tables: [{ name: "orders" }],
    });
  });
});

describe("renderFormattedToolResult", () => {
  const renderTool = (toolName: string, result: unknown) =>
    renderToStaticMarkup(
      ToolFormattedResult({ toolName, result, variant: "chat", showRawFallback: false }),
    );

  it("renders data tools with structured output instead of raw JSON", () => {
    expect(renderTool("list_data_sources", toolResultFixtures.list_data_sources.direct)).toContain(
      "API DuckDB Demo",
    );
    expect(renderTool("inspect_schema", toolResultFixtures.inspect_schema.direct)).toContain("orders");
    expect(renderTool("preview_table", toolResultFixtures.preview_table.direct)).toContain("orders");
    expect(renderTool("run_sql_readonly", toolResultFixtures.run_sql_readonly.flat)).toContain("128");
    expect(renderTool("run_sql_readonly", toolResultFixtures.run_sql_readonly.nested)).toContain("128");
    expect(renderTool("retrieve_knowledge", toolResultFixtures.retrieve_knowledge.direct)).toContain(
      "Refunds are processed",
    );
  });

  it("renders workspace tools from observation strings", () => {
    expect(renderTool("write_file", toolResultFixtures.write_file)).toContain("reports/summary.md");
    expect(renderTool("grep", toolResultFixtures.grep)).toContain("total revenue");
    expect(renderTool("execute_command", toolResultFixtures.execute_command)).toContain("verify-ok");
    expect(renderTool("execute_command", toolResultFixtures.execute_command_empty)).toContain(
      "no stdout",
    );
  });

  it("renders task and collaboration tools", () => {
    expect(renderTool("task_write", toolResultFixtures.task_write)).toContain("Inspect schema");
    expect(renderTool("ask_user", toolResultFixtures.ask_user)).toContain("Waiting for the user");
    expect(renderTool("submit_plan", toolResultFixtures.submit_plan)).toContain("Plan submitted");
  });

  it("unwraps observation-wrapped data tool payloads in the middle column", () => {
    expect(
      renderTool("list_data_sources", toolResultFixtures.list_data_sources.observationWrapped),
    ).toContain("API DuckDB Demo");
  });
});

describe("renderFormattedToolParams", () => {
  it("renders SQL and schema parameters as code blocks", () => {
    const sql = renderToStaticMarkup(
      renderFormattedToolParams(toolParametersFixtures.run_sql_readonly) as never,
    );
    expect(sql).toContain("SELECT id, total FROM orders LIMIT 5");

    const schema = renderToStaticMarkup(
      renderFormattedToolParams(toolParametersFixtures.inspect_schema) as never,
    );
    expect(schema).toContain("api-duckdb-demo");
    expect(schema).toContain("orders");
  });
});

describe("overview panel four-block plan", () => {
  it("shows all four overview blocks when run data is available", () => {
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
    expect(toolGroups).toHaveLength(2);
    expect(deriveProcessGroupUsage(toolGroups, liveRun).stepCount).toBe(2);
    expect(runUsage.toolCalls.byTool.list_data_sources?.calls).toBe(1);
    expect(runUsage.toolCalls.byTool.inspect_schema?.calls).toBe(1);
    expect(runUsage.toolCalls.byTool.run_sql_readonly?.calls).toBe(1);
  });

  it("keeps summary and progress visible before optional blocks appear", () => {
    const sections = overviewSectionPlan({
      hasToolDistribution: false,
    });
    expect(sections.map((section) => section.id)).toEqual(["conclusion", "progress"]);
  });
});

describe("toolResultObservationText", () => {
  it("returns readable text for wrapped and plain payloads", () => {
    expect(toolResultObservationText(toolResultFixtures.write_file)).toContain("Wrote 128 bytes");
    expect(parseToolResultRecord(toolResultFixtures.list_data_sources.observationWrapped)).toMatchObject({
      datasources: [{ id: "api-duckdb-demo" }],
    });
  });
});
