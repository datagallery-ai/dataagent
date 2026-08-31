import React from "react";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { buildTraceTimeline } from "../trace-timeline";
import { createInitialLiveRun, reduceLiveRunEvent } from "../live-run-state";
import {
  parseSchemaToolResult,
  parseSqlToolResult,
} from "../tool-result-normalize";
import {
  ToolFailureResult,
  ToolFormattedResult,
} from "../tool-result-format";
import {
  resolveToolFailurePresentation,
  toolResultLooksLikeError,
} from "../tool-call-display";
import { toolResultFixtures } from "../tool-display-fixtures";

const renderTool = (toolName: string, result: unknown, variant: "chat" | "console" = "console") =>
  renderToStaticMarkup(
    ToolFormattedResult({ toolName, result, variant, showRawFallback: false }),
  );

describe("observation-wrapped chat vs console parity", () => {
  const wrappedCases = [
    {
      tool: "list_data_sources",
      result: toolResultFixtures.list_data_sources.observationWrapped,
      expectText: "API DuckDB Demo",
    },
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
  ] as const;

  it.each(wrappedCases)("renders wrapped $tool in chat and console", ({ tool, result, expectText }) => {
    expect(renderTool(tool, result, "chat")).toContain(expectText);
    expect(renderTool(tool, result, "console")).toContain(expectText);
  });

  it("parses stringified payloads for chat card data paths", () => {
    expect(
      parseSqlToolResult(JSON.stringify(toolResultFixtures.run_sql_readonly.observationWrapped)),
    ).toMatchObject({ columns: ["total_orders"], row_count: 1 });
    expect(
      parseSchemaToolResult(JSON.stringify(toolResultFixtures.inspect_schema.observationWrapped)),
    ).toMatchObject({ tables: [{ name: "orders" }] });
  });
});

describe("failure presentation parity", () => {
  it("recognizes protocol and delivery errors", () => {
    const protocol = JSON.stringify(toolResultFixtures.protocolError);
    const delivery = JSON.stringify(toolResultFixtures.toolNotDelivered);
    expect(toolResultLooksLikeError(protocol)).toBe(true);
    expect(toolResultLooksLikeError(delivery)).toBe(true);
    expect(resolveToolFailurePresentation(protocol).title).toBe("Result sync failed");
    expect(resolveToolFailurePresentation(delivery).title).toBe("Result not delivered");
  });

  it("renders shared failure UI for console and chat paths", () => {
    const protocol = JSON.stringify(toolResultFixtures.protocolError);
    const markup = renderToStaticMarkup(
      ToolFailureResult({ toolName: "run_sql_readonly", result: protocol }),
    );
    expect(markup).toContain("Result sync failed");
    expect(markup).toContain("run_sql_readonly");
    expect(markup).not.toContain('"status": "error"');
  });
});

describe("empty and malformed tool payloads", () => {
  it("shows a zero-row message for empty SQL results", () => {
    const markup = renderTool("run_sql_readonly", toolResultFixtures.run_sql_readonly.emptyRows);
    expect(markup).toContain("The query returned no rows.");
    expect(markup).toContain("Rows");
  });

  it("keeps the middle-column SQL card on the shared result renderer", () => {
    const pageSource = readFileSync(new URL("../data-tasks-app.tsx", import.meta.url), "utf8");
    const sqlCardSource = pageSource.slice(
      pageSource.indexOf("function SqlToolCard"),
      pageSource.indexOf("type SchemaColumn"),
    );
    expect(sqlCardSource).toContain("<ToolFormattedResult");
    expect(sqlCardSource).toContain("toolName={name}");
    expect(sqlCardSource).toContain('variant="chat"');
  });

  it("returns null parsers for malformed schema payloads", () => {
    expect(parseSchemaToolResult(JSON.stringify({ datasource_id: "x" }))).toBeNull();
  });
});

describe("trace-aligned tool result rendering", () => {
  it("formats workspace tool raw results the same way trace cards should", () => {
    const markup = renderToStaticMarkup(
      ToolFormattedResult({
        toolName: "write_file",
        result: toolResultFixtures.write_file,
        variant: "console",
        showRawFallback: false,
      }),
    );
    expect(markup).toContain("reports/summary.md");
  });

  it("uses shared failure UI for errored trace payloads", () => {
    const protocol = JSON.stringify(toolResultFixtures.protocolError);
    const markup = renderToStaticMarkup(
      ToolFailureResult({ toolName: "run_sql_readonly", result: protocol }),
    );
    expect(markup).toContain("Result sync failed");
  });

  it("shows schema chips from wrapped restore payloads in timeline data", () => {
    let run = reduceLiveRunEvent(createInitialLiveRun(), { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tc-schema-wrap",
      toolCallName: "inspect_schema",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tc-schema-wrap",
      toolCallName: "inspect_schema",
      result: JSON.stringify(toolResultFixtures.inspect_schema.observationWrapped),
    });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });

    const entry = buildTraceTimeline(run).find((item) => item.toolCallId === "tc-schema-wrap");
    expect(entry?.schemaTables?.[0]?.name).toBe("orders");
  });
});
