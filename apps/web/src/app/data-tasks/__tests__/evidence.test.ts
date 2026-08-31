import { describe, expect, it } from "vitest";

import { buildEvidenceCardsFromLiveRun, evidenceChipLabel, evidenceChipTooltip, toggleEvidenceRef } from "../evidence";
import type { LiveRun } from "../live-run-state";

const baseRun = (): LiveRun => ({
  artifacts: [],
  audits: [],
  contextReports: [],
  events: [],
  plan: [],
  runId: "run-1",
  runStatus: "completed",
  sandboxOutputs: [],
  tokenUsageEvents: [],
  toolCalls: [],
  workspaceMetadata: [],
});

describe("evidence catalog", () => {
  it("maps dataset artifacts to table evidence refs", () => {
    const liveRun = baseRun();
    liveRun.artifacts = [
      {
        id: "artifact-1",
        kind: "csv",
        type: "dataset",
        title: "orders_by_region",
        summary: "Orders grouped by region.",
        detail: {
          type: "dataset",
          columns: ["region", "orders"],
          rows: [["East", "10"]],
        },
      },
    ];

    const cards = buildEvidenceCardsFromLiveRun(liveRun, "session-1");

    expect(cards).toHaveLength(1);
    expect(cards[0]?.ref).toMatchObject({
      id: "artifact:artifact-1",
      kind: "table",
      label: "orders_by_region",
      runId: "run-1",
      sessionId: "session-1",
      source: { artifactId: "artifact-1" },
    });
  });

  it("maps SQL tool calls to SQL and step evidence refs", () => {
    const liveRun = baseRun();
    liveRun.audits = [{ id: "audit-1", datasourceId: "db-1", rowCount: 2, status: "success" }];
    liveRun.toolCalls = [
      {
        id: "tool-1",
        name: "run_sql_readonly",
        result: JSON.stringify({ elapsed_ms: 12, row_count: 2, sql: "select * from orders" }),
        status: "success",
      },
    ];
    liveRun.events = [
      {
        id: "tool-1",
        kind: "query",
        payload: { scannedRows: 2, sql: "select * from orders" },
        summary: "Executed and returned 2 rows.",
        title: "Run SQL query",
        ts: "12:00:00",
      },
    ];

    const cards = buildEvidenceCardsFromLiveRun(liveRun, "session-1");
    const ids = cards.map((card) => card.ref.id);

    expect(ids).toContain("step:tool-1");
    expect(ids).toContain("sql-audit:audit-1");
    expect(cards.find((card) => card.ref.id === "sql-audit:audit-1")?.ref.source).toMatchObject({
      auditLogId: "audit-1",
      datasourceId: "db-1",
      toolCallId: "tool-1",
    });
  });

  it("toggles selected evidence by id", () => {
    const ref = {
      id: "artifact:artifact-1",
      kind: "table" as const,
      label: "orders_by_region",
      sessionId: "session-1",
      source: { artifactId: "artifact-1" },
    };

    expect(toggleEvidenceRef([], ref)).toEqual([ref]);
    expect(toggleEvidenceRef([ref], ref)).toEqual([]);
  });

  it("formats compact chip labels for long artifact titles and selections", () => {
    const tableRef = {
      id: "artifact:table-1",
      kind: "table" as const,
      label: "SQL result fd8b140f-4047-4882-afab-5e6e4dc5da5e.csv",
      sessionId: "session-1",
      source: {
        artifactId: "table-1",
        selection: { mode: "cells" as const, range: { r0: 0, c0: 0, r1: 5, c1: 2 } },
      },
    };
    const fileRef = {
      id: "artifact:file-1",
      kind: "file" as const,
      label: "reports/gmv_orders_weekly_comparison.md",
      sessionId: "session-1",
      source: {
        artifactId: "file-1",
        selection: {
          mode: "text" as const,
          quote: "GMV 与订单数周度对比报告 对比周期 基期 (W1)：2026-06-17 ~ 2026-06-23",
        },
      },
    };

    expect(evidenceChipLabel(tableRef)).toBe("Table · SQL result.csv · A1:C6");
    expect(evidenceChipLabel(fileRef)).toBe(
      'File · gmv_orders_weekly_comparison.md · "GMV 与订单数周度对比报告 对比周期 …"',
    );
    expect(evidenceChipTooltip(tableRef)).toContain("fd8b140f-4047-4882-afab-5e6e4dc5da5e.csv");
    expect(evidenceChipTooltip(fileRef)).toContain("2026-06-17");
  });
});
