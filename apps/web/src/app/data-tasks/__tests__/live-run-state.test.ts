import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  accumulateSessionUsage,
  artifactDetailFromPreview,
  artifactDetailNeedsPreviewFetch,
  createInitialLiveRun,
  createInitialSessionUsage,
  deriveLiveSessionView,
  deriveRunUsage,
  deriveSegmentRunUsage,
  formatSandboxOutputText,
  mergeArtifactDetail,
  resolveTokenUsageForToolCallIds,
  resolveSandboxOutputsForToolCall,
  resolveTokenUsageForEvent,
  resolveWorkspaceMetadataForToolCall,
  reduceLiveRunEvent,
  shouldSkipAgUiReplayDuringRestore,
} from "../live-run-state";

describe("live run state reducer", () => {
  it("tracks plan activity snapshots and deltas", () => {
    const initial = createInitialLiveRun();
    const withPlan = reduceLiveRunEvent(initial, {
      type: "ACTIVITY_SNAPSHOT",
      activityType: "PLAN",
      messageId: "run-1:activity:plan",
      replace: true,
      content: {
        tasks: [
          { id: "schema", title: "Inspect data source schema", status: "pending" },
          { id: "sql", title: "Generate and run read-only SQL", status: "pending" },
        ],
      },
    });

    const updated = reduceLiveRunEvent(withPlan, {
      type: "ACTIVITY_DELTA",
      activityType: "PLAN",
      messageId: "run-1:activity:plan",
      patch: [{ op: "replace", path: "/tasks/1/status", value: "running" }],
    });

    expect(updated.plan).toEqual([
      { id: "schema", title: "Inspect data source schema", status: "pending" },
      { id: "sql", title: "Generate and run read-only SQL", status: "running" },
    ]);
  });

  it("captures sql audit and artifact custom events", () => {
    const initial = createInitialLiveRun();
    const withAudit = reduceLiveRunEvent(initial, {
      type: "CUSTOM",
      name: "sql_audit",
      value: {
        audit_log_id: "audit-1",
        datasource_id: "api-duckdb-demo",
        status: "success",
        row_count: 12,
        elapsed_ms: 34,
      },
    });

    const updated = reduceLiveRunEvent(withAudit, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-1",
        title: "查询结果",
        summary: "12 行结果",
      },
    });

    expect(updated.audits[0]).toMatchObject({
      id: "audit-1",
      datasourceId: "api-duckdb-demo",
      rowCount: 12,
      elapsedMs: 34,
      status: "success",
    });
    expect(updated.artifacts[0]).toMatchObject({
      id: "artifact-1",
      title: "查询结果",
      summary: "12 行结果",
    });
  });

  it("captures session title custom events for chat title synchronization", () => {
    const updated = reduceLiveRunEvent(createInitialLiveRun(), {
      type: "CUSTOM",
      name: "session.title",
      value: {
        session_id: "thread-1",
        title: "渠道订单分析",
      },
    });

    expect(updated.sessionTitle).toEqual({
      sessionId: "thread-1",
      title: "渠道订单分析",
    });
  });

  it("tracks canceled run status from state delta and run finished events", () => {
    const started = reduceLiveRunEvent(createInitialLiveRun(), { type: "RUN_STARTED" });
    const canceledByDelta = reduceLiveRunEvent(started, {
      type: "STATE_DELTA",
      delta: [{ op: "replace", path: "/runStatus", value: "canceled" }],
    });

    expect(canceledByDelta.runStatus).toBe("canceled");

    const canceledByFinished = reduceLiveRunEvent(started, {
      type: "RUN_FINISHED",
      status: "cancelled",
    });

    expect(canceledByFinished.runStatus).toBe("canceled");
    expect(canceledByFinished.runFinishedAt).toBeTypeOf("number");
  });

  it("parses slim artifact references without inline preview data", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-slim-1",
        type: "table",
        name: "SQL result audit-1",
        title: "SQL result audit-1",
        summary: "数据集，100 行",
        preview_available: true,
      },
    });

    expect(run.artifacts[0]).toMatchObject({
      id: "artifact-slim-1",
      title: "SQL result audit-1",
      type: "dataset",
      summary: "数据集，100 行",
      previewAvailable: true,
    });
    expect(run.artifacts[0]?.detail).toBeUndefined();
  });

  it("keeps file artifact refs for download and @ file mentions", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-file-1",
        type: "file",
        name: "output/report.html",
        file_id: "file-ref-1",
        download_url: "/api/v1/artifacts/artifact-file-1/download",
        preview_json: {
          path: "output/report.html",
          size: 128,
        },
      },
    });

    expect(run.artifacts[0]).toMatchObject({
      id: "artifact-file-1",
      fileId: "file-ref-1",
      downloadUrl: "/api/v1/artifacts/artifact-file-1/download",
      detail: { type: "file", path: "output/report.html", size: 128 },
    });
  });

  it("parses table preview_json and links artifact to sql tool call", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-sql-1",
      toolCallName: "run_sql_readonly",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_END",
      toolCallId: "tool-sql-1",
      toolCallName: "run_sql_readonly",
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-1",
        type: "table",
        name: "SQL result audit-1",
        preview_json: {
          columns: ["id", "name"],
          rows: [
            ["1", "Alice"],
            ["2", "Bob"],
          ],
          row_count: 2,
        },
      },
    });

    expect(run.artifacts[0]).toMatchObject({
      id: "artifact-1",
      title: "SQL result audit-1",
      type: "dataset",
      createdByEventId: "tool-sql-1",
      detail: {
        type: "dataset",
        columns: ["id", "name"],
        rows: [
          ["1", "Alice"],
          ["2", "Bob"],
        ],
      },
    });
    expect(run.events.find((event) => event.id === "tool-sql-1")?.artifactIds).toEqual([
      "artifact-1",
    ]);
    expect(run.toolCalls[0]?.startedAtMs).toBeTypeOf("number");
    expect(run.toolCalls[0]?.finishedAtMs).toBeUndefined();
  });

  it("parses REST preview envelopes for dataset artifacts", () => {
    const artifact = {
      type: "dataset" as const,
      kind: "csv" as const,
      title: "SQL result audit-1",
    };
    const preview = {
      type: "table",
      preview_json: {
        columns: ["channel", "gmv"],
        rows: [{ channel: "search", gmv: 1280 }],
        row_count: 1,
      },
    };

    expect(artifactDetailFromPreview(artifact, preview)).toEqual({
      type: "dataset",
      columns: ["channel", "gmv"],
      rows: [["search", "1280"]],
    });
  });

  it("parses CSV string previews for table artifacts", () => {
    const artifact = {
      type: "dataset" as const,
      kind: "csv" as const,
      title: "category_sales_summary.csv",
    };
    const preview =
      "category,total_sales,percentage\nelectronics,1280,45.07\nbeauty,640,22.54";

    expect(artifactDetailFromPreview(artifact, preview)).toEqual({
      type: "dataset",
      columns: ["category", "total_sales", "percentage"],
      rows: [
        ["electronics", "1280", "45.07"],
        ["beauty", "640", "22.54"],
      ],
    });
  });

  it("parses markdown preview strings from REST preview payloads", () => {
    const artifact = {
      type: "report" as const,
      kind: "memo" as const,
      title: "Category Sales Summary",
    };

    expect(
      artifactDetailFromPreview(artifact, "# Category Sales Summary\n\nDone."),
    ).toEqual({
      type: "report",
      sections: [{ heading: "Preview", body: "# Category Sales Summary\n\nDone." }],
    });
  });

  it("parses markdown preview_json strings from artifact events", () => {
    const run = reduceLiveRunEvent(createInitialLiveRun(), {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-md-string-1",
        type: "markdown",
        name: "Category Sales Summary",
        preview_json: "# Category Sales Summary\n\nDone.",
      },
    });

    expect(run.artifacts[0]?.detail).toEqual({
      type: "report",
      sections: [{ heading: "Preview", body: "# Category Sales Summary\n\nDone." }],
    });
  });

  it("requests preview fetch for file artifacts that only have path metadata", () => {
    const artifact = {
      previewAvailable: true,
      type: "file" as const,
      kind: "file" as const,
      title: "report.md",
    };
    const detail = {
      type: "file" as const,
      path: "report.md",
      size: 128,
    };

    expect(artifactDetailNeedsPreviewFetch(artifact, detail)).toBe(true);
    expect(
      artifactDetailNeedsPreviewFetch(artifact, {
        ...detail,
        content: "# Hello",
      }),
    ).toBe(false);
  });

  it("requests preview fetch for file-backed markdown reports without preview_json", () => {
    const artifact = {
      previewAvailable: false,
      fileId: "file-1",
      type: "report" as const,
      kind: "memo" as const,
      title: "GMV report",
      id: "artifact-md-1",
    };
    expect(artifactDetailNeedsPreviewFetch(artifact, undefined)).toBe(true);
  });

  it("parses synthesized markdown preview envelopes from file-backed artifacts", () => {
    const artifact = {
      type: "report" as const,
      kind: "memo" as const,
      title: "GMV与订单数周度对比报告",
    };
    expect(
      artifactDetailFromPreview(artifact, {
        type: "markdown",
        content: "# GMV 对比\n\n本周上涨。",
        path: "reports/gmv.md",
      }),
    ).toEqual({
      type: "report",
      sections: [{ heading: "Preview", body: "# GMV 对比\n\n本周上涨。" }],
    });
  });

  it("merges fetched file preview content into existing file detail", () => {
    expect(
      mergeArtifactDetail(
        { type: "file", path: "report.md", size: 128 },
        { type: "file", path: "report.md", content: "# Hello" },
      ),
    ).toEqual({
      type: "file",
      path: "report.md",
      size: 128,
      content: "# Hello",
    });
  });

  it("parses markdown artifact preview_json into report detail", () => {
    const run = reduceLiveRunEvent(createInitialLiveRun(), {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-md-1",
        type: "markdown",
        name: "analysis.md",
        preview_json: {
          path: "analysis.md",
          content: "# Summary\n\nDone.",
        },
      },
    });

    expect(run.artifacts[0]?.detail).toEqual({
      type: "report",
      sections: [{ heading: "Preview", body: "# Summary\n\nDone." }],
    });
  });

  it("keeps the file artifact and drops a duplicate markdown report for the same workspace path", () => {
    let run = reduceLiveRunEvent(createInitialLiveRun(), {
      type: "TOOL_CALL_START",
      toolCallId: "tool-write-1",
      toolCallName: "write_file",
      args: { path: "weekly_comparison_report.md", content: "# Report" },
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-write-1",
      toolCallName: "write_file",
      result: JSON.stringify({ observation: "Wrote 8 bytes to weekly_comparison_report.md" }),
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-file-1",
        type: "file",
        name: "weekly_comparison_report.md",
        file_id: "file-1",
        preview_json: {
          path: "weekly_comparison_report.md",
          size: 8,
          tool: "write_file",
          content: "# Report",
        },
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-report-1",
        type: "markdown",
        name: "双周核心指标对比分析报告",
        preview_json: {
          path: "weekly_comparison_report.md",
          content: "# Report",
        },
      },
    });

    expect(run.artifacts).toHaveLength(1);
    expect(run.artifacts[0]).toMatchObject({
      id: "artifact-file-1",
      type: "file",
      title: "weekly_comparison_report.md",
      createdByEventId: "tool-write-1",
    });
  });

  it("links multiple sql artifacts to distinct tool calls", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });

    for (const [toolCallId, artifactId, value] of [
      ["tool-sql-1", "artifact-1", "A"],
      ["tool-sql-2", "artifact-2", "B"],
    ] as const) {
      run = reduceLiveRunEvent(run, {
        type: "TOOL_CALL_START",
        toolCallId,
        toolCallName: "run_sql_readonly",
      });
      run = reduceLiveRunEvent(run, {
        type: "TOOL_CALL_END",
        toolCallId,
        toolCallName: "run_sql_readonly",
      });
      run = reduceLiveRunEvent(run, {
        type: "CUSTOM",
        name: "artifact",
        value: {
          id: artifactId,
          type: "table",
          name: `Result ${value}`,
          preview_json: {
            columns: ["value"],
            rows: [[value]],
            row_count: 1,
          },
        },
      });
    }

    expect(run.events.find((event) => event.id === "tool-sql-1")?.artifactIds).toEqual([
      "artifact-1",
    ]);
    expect(run.events.find((event) => event.id === "tool-sql-2")?.artifactIds).toEqual([
      "artifact-2",
    ]);
    expect(run.artifacts.find((artifact) => artifact.id === "artifact-2")?.detail).toEqual({
      type: "dataset",
      columns: ["value"],
      rows: [["B"]],
    });
  });

  it("links batched sql artifacts to earliest unlinked sql tool after multiple tools", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });

    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-inspect-1",
      toolCallName: "inspect_schema",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-inspect-1",
      toolCallName: "inspect_schema",
      result: JSON.stringify({ tables: [{ name: "orders", columns: [] }] }),
    });

    for (const [toolCallId, rowCount] of [
      ["tool-sql-1", 2],
      ["tool-sql-2", 1],
    ] as const) {
      run = reduceLiveRunEvent(run, {
        type: "TOOL_CALL_START",
        toolCallId,
        toolCallName: "run_sql_readonly",
      });
      run = reduceLiveRunEvent(run, {
        type: "TOOL_CALL_RESULT",
        toolCallId,
        toolCallName: "run_sql_readonly",
        result: JSON.stringify({ row_count: rowCount, elapsed_ms: 5 }),
      });
    }

    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-1",
        type: "table",
        name: "Result A",
        preview_json: {
          columns: ["value"],
          rows: [["A1"], ["A2"]],
          row_count: 2,
        },
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-2",
        type: "table",
        name: "Result B",
        preview_json: {
          columns: ["value"],
          rows: [["B"]],
          row_count: 1,
        },
      },
    });

    expect(run.events.find((event) => event.id === "tool-sql-1")?.artifactIds).toEqual([
      "artifact-1",
    ]);
    expect(run.events.find((event) => event.id === "tool-sql-2")?.artifactIds).toEqual([
      "artifact-2",
    ]);
    expect(run.artifacts.find((artifact) => artifact.id === "artifact-1")?.detail).toEqual({
      type: "dataset",
      columns: ["value"],
      rows: [["A1"], ["A2"]],
    });
    expect(run.artifacts.find((artifact) => artifact.id === "artifact-2")?.detail).toEqual({
      type: "dataset",
      columns: ["value"],
      rows: [["B"]],
    });
  });

  it("captures an artifact custom event without fabricating extra state", () => {
    const initial = createInitialLiveRun();
    const withArtifact = reduceLiveRunEvent(initial, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-1",
        title: "查询结果",
        summary: "12 行结果",
      },
    });

    expect(withArtifact.artifacts).toHaveLength(1);
  });

  it("derives segment SQL usage from audits after the archived segment", () => {
    const run = {
      ...createInitialLiveRun(),
      runStatus: "completed" as const,
      runStartedAt: 3_000,
      runFinishedAt: 4_000,
      runHistory: [
        {
          startedAt: 1_000,
          finishedAt: 2_000,
          status: "completed" as const,
          toolCallEndIndex: 1,
          auditEndIndex: 1,
        },
      ],
      toolCalls: [
        { id: "tool-old", name: "run_sql_readonly", status: "success" as const },
        { id: "tool-current", name: "run_sql_readonly", status: "success" as const },
      ],
      audits: [
        {
          id: "audit-old",
          status: "success",
          rowCount: 10,
          elapsedMs: 5,
        },
        {
          id: "audit-current",
          status: "success",
          rowCount: 20,
          elapsedMs: 7,
        },
      ],
    };

    expect(deriveSegmentRunUsage(run).sql).toMatchObject({
      total: 1,
      success: 1,
      rowsScanned: 20,
      elapsedMs: 7,
    });
  });

  it("parses file artifacts and links them to write_file tool calls", () => {
    let run = reduceLiveRunEvent(createInitialLiveRun(), {
      type: "TOOL_CALL_START",
      toolCallId: "tool-write-1",
      toolCallName: "write_file",
      args: { path: "hello.txt", content: "hi" },
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-write-1",
      result: JSON.stringify({ observation: "Wrote 2 bytes to hello.txt" }),
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-file-1",
        type: "file",
        name: "hello.txt",
        preview_json: {
          path: "hello.txt",
          size: 2,
          mtime: "2026-06-23T09:00:00.000Z",
          tool: "write_file",
        },
      },
    });

    expect(run.artifacts[0]).toMatchObject({
      id: "artifact-file-1",
      type: "file",
      kind: "file",
      detail: {
        type: "file",
        path: "hello.txt",
        size: 2,
        tool: "write_file",
      },
    });
    expect(run.events.find((event) => event.id === "tool-write-1")?.artifactIds).toEqual([
      "artifact-file-1",
    ]);
  });

  it("links publish_artifact file outputs to the publish tool instead of leaving them orphaned", () => {
    let run = reduceLiveRunEvent(createInitialLiveRun(), {
      type: "TOOL_CALL_START",
      toolCallId: "tool-write-1",
      toolCallName: "write_file",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-write-1",
      toolCallName: "write_file",
      result: JSON.stringify({ observation: "Wrote 2 bytes to test_file.txt" }),
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-write",
        type: "file",
        name: "test_file.txt",
        preview_json: { path: "test_file.txt", size: 2, tool: "write_file" },
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-publish-1",
      toolCallName: "publish_artifact",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-publish-1",
      toolCallName: "publish_artifact",
      result: JSON.stringify({ observation: "Published test_file.txt" }),
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-publish",
        type: "file",
        name: "test_file.txt",
        preview_json: { path: "test_file.txt", size: 2, tool: "publish_artifact" },
      },
    });

    expect(run.artifacts.find((artifact) => artifact.id === "artifact-publish")?.createdByEventId).toBe(
      "tool-publish-1",
    );
    expect(run.events.find((event) => event.id === "tool-publish-1")?.artifactIds).toEqual([
      "artifact-publish",
    ]);
  });

  it("captures run diagnostic custom events for overview display", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "skill.selection",
      value: {
        mode: "auto",
        selected: [{ id: "skill-sql", name: "SQL Analyst", revision: 3 }],
        effective_tool_policy: { allowedTools: ["inspect_schema", "run_sql_readonly"] },
        audit: [{ skillId: "skill-sql", decision: "selected", reasons: ["query:analysis"] }],
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "goal.updated",
      value: { objective: "分析订单渠道", source: "user", status: "running" },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "run.config.resolved",
      value: {
        activeDatasourceId: "orders-db",
        enabledKnowledgeIds: ["kb-orders"],
        enabledMcpServerIds: ["mcp-local"],
        selectedSkills: [{ id: "skill-sql", name: "SQL Analyst" }],
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "context.compiled",
      value: { step: "prepare", token_report: { total_tokens: 1200, budget_tokens: 8000 } },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "context.prompt-verified",
      value: { model: "qwen-plus", prompt_tokens: 900, remaining_tokens: 7100 },
    });

    expect((run as any).skillSelection).toMatchObject({
      mode: "auto",
      selected: [{ id: "skill-sql", name: "SQL Analyst", revision: 3 }],
    });
    expect((run as any).goal).toMatchObject({
      objective: "分析订单渠道",
      source: "user",
      status: "running",
    });
    expect((run as any).resolvedRunConfig).toMatchObject({
      activeDatasourceId: "orders-db",
      enabledKnowledgeIds: ["kb-orders"],
      enabledMcpServerIds: ["mcp-local"],
    });
    expect((run as any).contextReports).toHaveLength(2);
    expect((run as any).contextReports[0]).toMatchObject({
      name: "context.prompt-verified",
      value: { model: "qwen-plus" },
    });
  });

  it("parses snake_case run.config.resolved fields from backend events", () => {
    const run = reduceLiveRunEvent(createInitialLiveRun(), {
      type: "CUSTOM",
      name: "run.config.resolved",
      value: {
        active_datasource_id: "orders-db",
        requested_llm_profile_id: "qwen-plus",
        enabled_knowledge_ids: ["kb-orders"],
        enabled_mcp_server_ids: ["mcp-local"],
        file_ids: ["file-1"],
        selected_skill_ids: ["skill-sql"],
      },
    });

    expect((run as any).resolvedRunConfig).toMatchObject({
      activeDatasourceId: "orders-db",
      activeLlmProfileId: "qwen-plus",
      enabledKnowledgeIds: ["kb-orders"],
      enabledMcpServerIds: ["mcp-local"],
      fileIds: ["file-1"],
      selectedSkills: [{ id: "skill-sql" }],
    });
  });

  it("parses chart artifact preview_json into renderable chart detail", () => {
    const run = reduceLiveRunEvent(createInitialLiveRun(), {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-chart-1",
        type: "chart",
        title: "渠道订单量",
        preview_json: {
          chartType: "bar",
          unit: "单",
          points: [
            { label: "search", value: 42 },
            { label: "direct", value: 18 },
          ],
        },
      },
    });

    expect(run.artifacts[0]).toMatchObject({
      id: "artifact-chart-1",
      type: "chart",
      kind: "chart",
      detail: {
        type: "chart",
        chartType: "bar",
        unit: "单",
        points: [
          { label: "search", value: 42 },
          { label: "direct", value: 18 },
        ],
      },
    });
  });

  it("links write_file and edit_file artifacts to distinct tool calls", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });

    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-write-1",
      toolCallName: "write_file",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-write-1",
      toolCallName: "write_file",
      result: JSON.stringify({ observation: "Wrote 47 bytes to test_dir/test.txt" }),
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-write",
        type: "file",
        name: "test_dir/test.txt",
        preview_json: {
          path: "test_dir/test.txt",
          size: 47,
          tool: "write_file",
        },
      },
    });

    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-edit-1",
      toolCallName: "edit_file",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-edit-1",
      toolCallName: "edit_file",
      result: JSON.stringify({
        observation: "Replaced 1 occurrence in test_dir/test.txt",
      }),
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-edit",
        type: "file",
        name: "test_dir/test.txt",
        preview_json: {
          path: "test_dir/test.txt",
          size: 46,
          tool: "edit_file",
        },
      },
    });

    expect(run.artifacts.find((artifact) => artifact.id === "artifact-write")?.createdByEventId).toBe(
      "tool-write-1",
    );
    expect(run.artifacts.find((artifact) => artifact.id === "artifact-edit")?.createdByEventId).toBe(
      "tool-edit-1",
    );
    expect(run.events.find((event) => event.id === "tool-write-1")?.artifactIds).toEqual([
      "artifact-write",
    ]);
    expect(run.events.find((event) => event.id === "tool-edit-1")?.artifactIds).toEqual([
      "artifact-edit",
    ]);
  });

  it("links file artifact when CUSTOM arrives before TOOL_CALL_RESULT", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-write-1",
      toolCallName: "write_file",
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-write",
        type: "file",
        name: "test_dir/test.txt",
        preview_json: {
          path: "test_dir/test.txt",
          size: 47,
          tool: "write_file",
        },
      },
    });
    expect(run.artifacts[0]?.createdByEventId).toBeUndefined();

    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-write-1",
      toolCallName: "write_file",
      result: JSON.stringify({ observation: "Wrote 47 bytes to test_dir/test.txt" }),
    });

    expect(run.artifacts.find((artifact) => artifact.id === "artifact-write")?.createdByEventId).toBe(
      "tool-write-1",
    );
  });

  it("does not link edit_file artifact to write_file when write_file is still unlinked", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });

    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-write-1",
      toolCallName: "write_file",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-write-1",
      toolCallName: "write_file",
      result: JSON.stringify({ observation: "Wrote 47 bytes to test_dir/test.txt" }),
    });

    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-edit-1",
      toolCallName: "edit_file",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-edit-1",
      toolCallName: "edit_file",
      result: JSON.stringify({
        observation: "Replaced 1 occurrence in test_dir/test.txt",
      }),
    });

    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-write",
        type: "file",
        name: "test_dir/test.txt",
        preview_json: { path: "test_dir/test.txt", size: 47, tool: "write_file" },
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-edit",
        type: "file",
        name: "test_dir/test.txt",
        preview_json: { path: "test_dir/test.txt", size: 46, tool: "edit_file" },
      },
    });

    expect(run.artifacts.find((artifact) => artifact.id === "artifact-write")?.createdByEventId).toBe(
      "tool-write-1",
    );
    expect(run.artifacts.find((artifact) => artifact.id === "artifact-edit")?.createdByEventId).toBe(
      "tool-edit-1",
    );
  });

  it("preserves run progress when RUN_STARTED resumes after suspension", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-inspect-1",
      toolCallName: "inspect_schema",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-inspect-1",
      toolCallName: "inspect_schema",
      result: JSON.stringify({ tables: [{ name: "orders", columns: [] }] }),
    });
    run = reduceLiveRunEvent(run, {
      type: "STATE_DELTA",
      delta: [{ op: "replace", path: "/runStatus", value: "suspended" }],
    });

    const resumed = reduceLiveRunEvent(run, { type: "RUN_STARTED" });

    expect(resumed.runStatus).toBe("running");
    expect(resumed.toolCalls).toHaveLength(1);
    expect(resumed.events).toHaveLength(1);
    expect(resumed.toolCalls[0]?.name).toBe("inspect_schema");
  });

  it("preserves orphan session artifacts when a new run starts", () => {
    const seeded = reduceLiveRunEvent(createInitialLiveRun(), {
      type: "CUSTOM",
      name: "artifact",
      value: { id: "artifact-1", title: "旧产出", summary: "" },
    });

    const restarted = reduceLiveRunEvent(seeded, { type: "RUN_STARTED" });

    expect(restarted.artifacts).toHaveLength(1);
    expect(restarted.events).toEqual([]);
    expect(restarted.runStatus).toBe("running");
  });

  it("accumulates session tool calls when a new run starts in the same thread", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-1",
      toolCallName: "inspect_schema",
    });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });

    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });

    expect(run.toolCalls).toHaveLength(1);
    expect(run.runHistory).toHaveLength(1);
    expect(run.runHistory?.[0]?.toolCallEndIndex).toBe(1);
    expect(run.runStatus).toBe("running");
    expect(run.runFinishedAt).toBeUndefined();

    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-2",
      toolCallName: "run_sql_readonly",
    });

    expect(run.toolCalls).toHaveLength(2);
  });

  it("clears stale run diagnostics when a new run starts after failure", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "run.config.resolved",
      value: {
        activeDatasourceId: "orders-db",
        activeLlmProfileId: "bad-model",
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "skill.selection",
      value: { mode: "auto", selected: [{ id: "skill-sql" }], audit: [] },
    });
    run = reduceLiveRunEvent(run, {
      type: "RUN_ERROR",
      message: "PROVIDER_CONFIG_MISSING:bad-model",
    });

    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });

    expect(run.runStatus).toBe("running");
    expect(run.resolvedRunConfig).toBeUndefined();
    expect(run.skillSelection).toBeUndefined();
    expect(run.errorMessage).toBeUndefined();
  });

  it("ignores duplicate RUN_STARTED while the run is already running", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED", runId: "run-1" });
    const startedAt = run.runStartedAt;
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-1",
      toolCallName: "inspect_schema",
    });

    const replayed = reduceLiveRunEvent(run, { type: "RUN_STARTED", runId: "run-1" });

    expect(replayed.runStartedAt).toBe(startedAt);
    expect(replayed.toolCalls).toHaveLength(1);
    expect(replayed.runHistory ?? []).toHaveLength(0);
  });

  it("ignores duplicate RUN_FINISHED after the run already completed", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
    const finishedAt = run.runFinishedAt;

    const replayed = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });

    expect(replayed.runFinishedAt).toBe(finishedAt);
    expect(replayed.runStatus).toBe("completed");
  });

  it("preserves tool success when TOOL_CALL_RESULT arrives before START/END", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tc-out-of-order",
      toolCallName: "inspect_schema",
      result: JSON.stringify({ tables: [{ name: "orders", columns: [] }] }),
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tc-out-of-order",
      toolCallName: "inspect_schema",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_END",
      toolCallId: "tc-out-of-order",
      toolCallName: "inspect_schema",
    });

    expect(run.toolCalls[0]?.status).toBe("success");
  });

  describe("tool e2e duration idempotency", () => {
    beforeEach(() => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date("2026-07-09T04:00:00.000Z"));
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it("does not refresh finishedAtMs when TOOL_CALL_RESULT is replayed", () => {
      let run = createInitialLiveRun();
      run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
      run = reduceLiveRunEvent(run, {
        type: "TOOL_CALL_START",
        toolCallId: "tc-write",
        toolCallName: "write_file",
      });
      vi.setSystemTime(new Date("2026-07-09T04:00:00.023Z"));
      run = reduceLiveRunEvent(run, {
        type: "TOOL_CALL_RESULT",
        toolCallId: "tc-write",
        toolCallName: "write_file",
        result: JSON.stringify({ path: "report.txt", size: 12 }),
      });

      const startedAtMs = run.toolCalls[0]?.startedAtMs;
      const finishedAtMs = run.toolCalls[0]?.finishedAtMs;
      expect(startedAtMs).toBe(Date.parse("2026-07-09T04:00:00.000Z"));
      expect(finishedAtMs).toBe(Date.parse("2026-07-09T04:00:00.023Z"));
      expect((finishedAtMs ?? 0) - (startedAtMs ?? 0)).toBe(23);

      // Session switch-back: AG-UI replays RESULT much later.
      vi.setSystemTime(new Date("2026-07-09T04:00:45.000Z"));
      const replayed = reduceLiveRunEvent(run, {
        type: "TOOL_CALL_RESULT",
        toolCallId: "tc-write",
        toolCallName: "write_file",
        result: JSON.stringify({ path: "report.txt", size: 12 }),
      });

      expect(replayed.toolCalls[0]?.startedAtMs).toBe(startedAtMs);
      expect(replayed.toolCalls[0]?.finishedAtMs).toBe(finishedAtMs);
      expect(
        (replayed.toolCalls[0]?.finishedAtMs ?? 0) - (replayed.toolCalls[0]?.startedAtMs ?? 0),
      ).toBe(23);
    });

    it("skips completed-tool AG-UI replay while restoring conversation", () => {
      let run = createInitialLiveRun();
      run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
      run = reduceLiveRunEvent(run, {
        type: "TOOL_CALL_START",
        toolCallId: "tc-sql",
        toolCallName: "run_sql_readonly",
      });
      vi.setSystemTime(new Date("2026-07-09T04:00:00.040Z"));
      run = reduceLiveRunEvent(run, {
        type: "TOOL_CALL_RESULT",
        toolCallId: "tc-sql",
        toolCallName: "run_sql_readonly",
        result: JSON.stringify({ row_count: 1 }),
      });

      expect(
        shouldSkipAgUiReplayDuringRestore(run, {
          type: "TOOL_CALL_RESULT",
          toolCallId: "tc-sql",
        }),
      ).toBe(true);
      expect(
        shouldSkipAgUiReplayDuringRestore(run, {
          type: "TOOL_CALL_END",
          toolCallId: "tc-sql",
        }),
      ).toBe(true);
      expect(
        shouldSkipAgUiReplayDuringRestore(run, {
          type: "RUN_FINISHED",
        }),
      ).toBe(true);
      expect(
        shouldSkipAgUiReplayDuringRestore(run, {
          type: "TOOL_CALL_START",
          toolCallId: "tc-new",
          toolCallName: "inspect_schema",
        }),
      ).toBe(false);
    });
  });

  it("finalizes running tool calls when STATE_DELTA completes before RUN_FINISHED", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tc-inspect",
      toolCallName: "inspect_schema",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_END",
      toolCallId: "tc-inspect",
      toolCallName: "inspect_schema",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tc-sql",
      toolCallName: "run_sql_readonly",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tc-sql",
      toolCallName: "run_sql_readonly",
      result: JSON.stringify({ row_count: 2 }),
    });
    expect(run.toolCalls.find((call) => call.id === "tc-inspect")?.status).toBe("running");

    run = reduceLiveRunEvent(run, {
      type: "STATE_DELTA",
      delta: [{ op: "replace", path: "/runStatus", value: "completed" }],
    });
    expect(run.runStatus).toBe("completed");
    expect(run.toolCalls.find((call) => call.id === "tc-inspect")?.status).toBe("success");

    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
    expect(run.runStatus).toBe("completed");
    expect(run.toolCalls.every((call) => call.status !== "running")).toBe(true);
  });

  it("does not append duplicate archived segments when RUN_STARTED replays without new tools", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-1",
      toolCallName: "inspect_schema",
    });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });

    expect(run.runHistory).toHaveLength(1);
    expect(run.runHistory?.[0]?.toolCallEndIndex).toBe(1);
  });

  it("resets to an idle empty run on first RUN_STARTED with no session activity", () => {
    const restarted = reduceLiveRunEvent(createInitialLiveRun(), { type: "RUN_STARTED" });

    expect(restarted.artifacts).toEqual([]);
    expect(restarted.events).toEqual([]);
    expect(restarted.toolCalls).toEqual([]);
    expect(restarted.runStatus).toBe("running");
  });

  it("keeps tool call execution details in timeline events", () => {
    const initial = createInitialLiveRun();
    const withArgs = reduceLiveRunEvent(initial, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-1",
      toolCallName: "run_sql_readonly",
      args: {
        sql: "SELECT count(*) AS total FROM orders",
      },
    });

    const updated = reduceLiveRunEvent(withArgs, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-1",
      toolCallName: "run_sql_readonly",
      result: "total: 42",
    });

    expect(updated.events[0]).toMatchObject({
      id: "tool-1",
      kind: "query",
      toolName: "run_sql_readonly",
      title: "Run SQL query",
      summary: "total: 42",
      payload: {
        sql: "SELECT count(*) AS total FROM orders",
      },
    });
    expect(updated.toolCalls[0]).toMatchObject({
      id: "tool-1",
      name: "run_sql_readonly",
      status: "success",
      result: "total: 42",
      startedAtMs: expect.any(Number),
      finishedAtMs: expect.any(Number),
    });
  });

  it("derives run usage and accumulates session stats", () => {
    let run = reduceLiveRunEvent(createInitialLiveRun(), {
      type: "RUN_STARTED",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-1",
      toolCallName: "run_sql_readonly",
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "sql_audit",
      value: {
        audit_log_id: "audit-1",
        status: "success",
        row_count: 100,
        elapsed_ms: 50,
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: { id: "artifact-1", title: "结果", summary: "" },
    });
    run = reduceLiveRunEvent(run, {
      type: "RUN_FINISHED",
    });

    const runUsage = deriveRunUsage(run);
    expect(runUsage.toolCalls.total).toBe(1);
    expect(runUsage.sql.total).toBe(1);
    expect(runUsage.sql.rowsScanned).toBe(100);
    expect(runUsage.artifactCount).toBe(1);

    const session = accumulateSessionUsage(
      createInitialSessionUsage(),
      runUsage,
      "completed",
    );
    expect(session.runCount).toBe(1);
    expect(session.completedRuns).toBe(1);
    expect(session.toolCalls.total).toBe(1);
    expect(session.sql.rowsScanned).toBe(100);
    expect(session.artifactCount).toBe(1);
  });

  it("deriveLiveSessionView merges in-progress run without double-counting completed", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-1",
      toolCallName: "run_sql_readonly",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_END",
      toolCallId: "tool-1",
    });

    const session = createInitialSessionUsage();
    const view = deriveLiveSessionView(session, run);
    expect(view.includesInProgressRun).toBe(true);
    expect(view.runCount).toBe(1);
    expect(view.toolCalls.total).toBe(1);

    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
    const runUsage = deriveRunUsage(run);
    const accumulated = accumulateSessionUsage(session, runUsage, "completed");
    const settled = deriveLiveSessionView(accumulated, run);
    expect(settled.includesInProgressRun).toBe(false);
    expect(settled.runCount).toBe(1);
    expect(settled.toolCalls.total).toBe(1);
  });

  it("accumulates token_usage custom events into run and session stats", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "token_usage",
      value: { input_tokens: 1200, output_tokens: 340 },
    });

    const runUsage = deriveRunUsage(run);
    expect(runUsage.tokenUsageReported).toBe(true);
    expect(runUsage.tokens.inputTokens).toBe(1200);
    expect(runUsage.tokens.outputTokens).toBe(340);

    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
    const finishedUsage = deriveRunUsage(run);
    const session = accumulateSessionUsage(
      createInitialSessionUsage(),
      finishedUsage,
      "completed",
    );
    expect(session.tokenUsageReported).toBe(true);
    expect(session.tokens.inputTokens).toBe(1200);
    expect(session.tokens.outputTokens).toBe(340);
  });

  it("does not double count duplicate token_usage custom events", () => {
    let run = createInitialLiveRun();
    const event = {
      type: "CUSTOM" as const,
      name: "token_usage",
      value: {
        step_number: 1,
        model: "qwen-plus",
        input_tokens: 1200,
        output_tokens: 340,
      },
    };

    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, event);
    run = reduceLiveRunEvent(run, event);

    const runUsage = deriveRunUsage(run);
    expect(runUsage.tokens.inputTokens).toBe(1200);
    expect(runUsage.tokens.outputTokens).toBe(340);
    expect(run.tokenUsageEvents).toHaveLength(1);
  });

  it("tracks token_usage model, cost, and step-level usage", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-sql-1",
      toolCallName: "run_sql_readonly",
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "token_usage",
      value: {
        step_number: 1,
        model: "qwen-plus",
        input_tokens: 1000,
        output_tokens: 250,
        cost_usd: 0.0123,
      },
    });

    const runUsage = deriveRunUsage(run);
    expect(runUsage.tokens).toMatchObject({
      inputTokens: 1000,
      outputTokens: 250,
      costUsd: 0.0123,
    });
    expect(runUsage.models).toEqual(["qwen-plus"]);

    const stepUsage = resolveTokenUsageForEvent(run, run.events[0] ?? null);
    expect(stepUsage).toMatchObject({
      reported: true,
      inputTokens: 1000,
      outputTokens: 250,
      costUsd: 0.0123,
      models: ["qwen-plus"],
      approximate: true,
    });
  });

  it("prefers tool_call_id over step_number when both are present", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-sql-1",
      toolCallName: "run_sql_readonly",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-sql-2",
      toolCallName: "run_sql_readonly",
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "token_usage",
      value: {
        tool_call_id: "tool-sql-2",
        input_tokens: 50,
        output_tokens: 10,
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "token_usage",
      value: {
        step_number: 1,
        input_tokens: 900,
        output_tokens: 100,
      },
    });

    const firstStepUsage = resolveTokenUsageForEvent(run, run.events[0] ?? null);
    const secondStepUsage = resolveTokenUsageForEvent(run, run.events[1] ?? null);

    expect(firstStepUsage).toMatchObject({
      inputTokens: 900,
      outputTokens: 100,
      approximate: true,
    });
    expect(secondStepUsage).toMatchObject({
      inputTokens: 50,
      outputTokens: 10,
      approximate: undefined,
    });
  });

  it("keeps a later ACTIVITY STEP as its own orphan until its TOOL_CALL_START arrives", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "call_sql_1",
      toolCallName: "run_sql_readonly",
    });
    run = reduceLiveRunEvent(run, {
      type: "ACTIVITY_SNAPSHOT",
      activityType: "STEP",
      content: {
        step_id: "sql-1",
        title: "Run read-only SQL",
        tool_name: "run_sql_readonly",
        status: "completed",
        sql: "SELECT 1",
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "call_sql_1",
      toolCallName: "run_sql_readonly",
      result: JSON.stringify({ row_count: 1, elapsed_ms: 2 }),
    });

    // Next SQL ACTIVITY arrives before its tool envelope — must not overwrite sql-1.
    run = reduceLiveRunEvent(run, {
      type: "ACTIVITY_SNAPSHOT",
      activityType: "STEP",
      content: {
        step_id: "sql-2",
        title: "Run read-only SQL",
        tool_name: "run_sql_readonly",
        status: "completed",
        sql: "SELECT 2",
      },
    });
    expect(run.events.map((event) => event.id).sort()).toEqual(["call_sql_1", "sql-2"]);
    expect(run.events.find((event) => event.id === "call_sql_1")?.payload).toMatchObject({
      sql: "SELECT 1",
    });
    expect(run.events.find((event) => event.id === "sql-2")?.payload).toMatchObject({
      sql: "SELECT 2",
    });

    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "call_sql_2",
      toolCallName: "run_sql_readonly",
      args: { sql: "SELECT 2" },
    });
    expect(run.events.map((event) => event.id).sort()).toEqual(["call_sql_1", "call_sql_2"]);
    expect(run.toolCalls.map((call) => call.stepId)).toEqual(["sql-1", "sql-2"]);
  });

  it("absorbs ACTIVITY STEP events created before TOOL_CALL_START into the tool call id", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    // Backend emits ACTIVITY before the AG-UI tool-call envelope (real event order).
    run = reduceLiveRunEvent(run, {
      type: "ACTIVITY_SNAPSHOT",
      activityType: "STEP",
      content: {
        step_id: "schema-d2d92392",
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
      toolCallId: "call_inspect_1",
      toolCallName: "inspect_schema",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "call_inspect_1",
      toolCallName: "inspect_schema",
      result: JSON.stringify({
        tables: [{ name: "orders", columns: [{ name: "id", type: "TEXT" }] }],
      }),
    });

    expect(run.events.map((event) => event.id)).toEqual(["call_inspect_1"]);
    expect(run.events[0]).toMatchObject({
      id: "call_inspect_1",
      stepId: "schema-d2d92392",
      toolName: "inspect_schema",
    });
    expect(run.toolCalls[0]).toMatchObject({
      id: "call_inspect_1",
      stepId: "schema-d2d92392",
    });
  });

  it("applies token_usage.correlation to attach step_id for detail matching", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-sql-1",
      toolCallName: "run_sql_readonly",
    });
    run = reduceLiveRunEvent(run, {
      type: "ACTIVITY_SNAPSHOT",
      activityType: "STEP",
      content: {
        step_id: "sql-1",
        title: "Run read-only SQL",
        tool_name: "run_sql_readonly",
        status: "running",
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "token_usage",
      value: {
        tool_call_id: "tool-sql-1",
        input_tokens: 800,
        output_tokens: 120,
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "token_usage.correlation",
      value: {
        step_id: "sql-1",
        tool_call_id: "tool-sql-1",
        tool_name: "run_sql_readonly",
      },
    });

    const stepUsage = resolveTokenUsageForEvent(run, run.events[0] ?? null);
    expect(stepUsage).toMatchObject({
      reported: true,
      inputTokens: 800,
      outputTokens: 120,
      approximate: undefined,
    });
  });

  it("aggregates unique token usage records for multi-tool groups", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-a",
      toolCallName: "inspect_schema",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-b",
      toolCallName: "run_sql_readonly",
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "token_usage",
      value: {
        tool_call_id: "tool-a",
        input_tokens: 100,
        output_tokens: 20,
        model: "qwen-plus",
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "token_usage",
      value: {
        tool_call_id: "tool-a",
        input_tokens: 100,
        output_tokens: 20,
        model: "qwen-plus",
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "token_usage",
      value: {
        tool_call_id: "tool-b",
        input_tokens: 300,
        output_tokens: 40,
        model: "qwen-plus",
      },
    });

    const usage = resolveTokenUsageForToolCallIds(run, ["tool-a", "tool-b"]);
    expect(usage).toMatchObject({
      reported: true,
      inputTokens: 400,
      outputTokens: 60,
      models: ["qwen-plus"],
    });
  });

  it("resets token usage when a new run starts in the same thread", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "token_usage",
      value: { input_tokens: 1000, output_tokens: 200 },
    });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });

    const firstRunUsage = deriveRunUsage(run);
    const session = accumulateSessionUsage(
      createInitialSessionUsage(),
      deriveSegmentRunUsage(run),
      "completed",
    );
    expect(session.tokens.inputTokens).toBe(1000);

    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "token_usage",
      value: { input_tokens: 300, output_tokens: 50 },
    });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });

    const secondSession = accumulateSessionUsage(
      session,
      deriveSegmentRunUsage(run),
      "completed",
    );
    expect(deriveRunUsage(run).tokens.inputTokens).toBe(300);
    expect(secondSession.tokens.inputTokens).toBe(1300);
    expect(firstRunUsage.tokens.inputTokens).toBe(1000);
  });

  it("preserves sql tool identity when bridged TOOL_CALL_RESULT omits toolCallName", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-sql-1",
      toolCallName: "run_sql_readonly",
      args: { sql: "SELECT * FROM orders LIMIT 10" },
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_END",
      toolCallId: "tool-sql-1",
      toolCallName: "run_sql_readonly",
    });
    run = reduceLiveRunEvent(run, {
      type: "ACTIVITY_SNAPSHOT",
      activityType: "STEP",
      content: {
        step_id: "sql-1",
        title: "Run read-only SQL",
        tool_name: "run_sql_readonly",
        status: "completed",
        output_type: "table",
        content: {
          columns: ["order_id"],
          rows: [["1"]],
          row_count: 1,
        },
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-1",
        type: "table",
        name: "SQL result audit-1",
        preview_json: {
          columns: ["order_id"],
          rows: [["1"]],
          row_count: 1,
        },
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-sql-1",
      content: JSON.stringify({
        columns: ["order_id"],
        rows: [["1"]],
        row_count: 1,
        elapsed_ms: 6,
      }),
    });

    expect(run.toolCalls[0]).toMatchObject({
      id: "tool-sql-1",
      name: "run_sql_readonly",
      stepId: "sql-1",
      status: "success",
    });
    expect(run.events.find((event) => event.id === "tool-sql-1")).toMatchObject({
      kind: "query",
      toolName: "run_sql_readonly",
      artifactIds: ["artifact-1"],
      stepId: "sql-1",
    });
    expect(run.artifacts[0]?.createdByEventId).toBe("tool-sql-1");
  });

  it("correlates ACTIVITY STEP with toolCallId and keeps failed status consistent", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-sql-1",
      toolCallName: "run_sql_readonly",
      args: { sql: "SELECT 1" },
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_END",
      toolCallId: "tool-sql-1",
      toolCallName: "run_sql_readonly",
    });
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
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });

    expect(run.events).toHaveLength(1);
    expect(run.events[0]).toMatchObject({
      id: "tool-sql-1",
      stepId: "sql-1",
      activityStatus: "failed",
      summary: "Failed.",
      payload: {
        sql: "SELECT 1",
        errorMessage: "permission denied",
      },
    });
    expect(run.toolCalls[0]).toMatchObject({
      id: "tool-sql-1",
      stepId: "sql-1",
      status: "failed",
    });
  });

  it("links an artifact to its authoritative tool_call_id, ignoring heuristics (R-018)", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });

    // Two successful SQL calls with the SAME row_count so heuristics would be ambiguous.
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-sql-1",
      toolCallName: "run_sql_readonly",
      args: { sql: "SELECT * FROM a" },
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-sql-1",
      toolCallName: "run_sql_readonly",
      result: JSON.stringify({ audit_log_id: "audit-1", row_count: 5, elapsed_ms: 3 }),
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-sql-2",
      toolCallName: "run_sql_readonly",
      args: { sql: "SELECT * FROM b" },
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-sql-2",
      toolCallName: "run_sql_readonly",
      result: JSON.stringify({ audit_log_id: "audit-2", row_count: 5, elapsed_ms: 3 }),
    });

    // Artifact carries the authoritative producing tool_call_id (the SECOND call).
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-authoritative",
        type: "table",
        name: "SQL result audit-2",
        tool_call_id: "tool-sql-2",
        preview_json: { audit_log_id: "audit-2", row_count: 5 },
      },
    });

    expect(run.artifacts[0]?.createdByEventId).toBe("tool-sql-2");
    expect(run.artifacts[0]?.createdByToolCallId).toBe("tool-sql-2");
    expect(run.events.find((event) => event.id === "tool-sql-2")?.artifactIds).toEqual([
      "artifact-authoritative",
    ]);
    expect(run.events.find((event) => event.id === "tool-sql-1")?.artifactIds).toBeUndefined();
  });

  it("does not link sql artifact to a failed tool call when artifact arrives after failure", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });

    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-sql-1",
      toolCallName: "run_sql_readonly",
      args: { sql: "SELECT * FROM orders" },
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-sql-1",
      toolCallName: "run_sql_readonly",
      result: JSON.stringify({
        audit_log_id: "audit-success",
        row_count: 3,
        elapsed_ms: 6,
      }),
    });

    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-sql-2",
      toolCallName: "run_sql_readonly",
      args: { sql: "SELECT channel, COUNT(*) AS count FROM orders GROUP BY channel" },
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tool-sql-2",
      toolCallName: "run_sql_readonly",
      result: JSON.stringify({
        status: "error",
        error: "Only simple SELECT column list FROM table queries are supported for file/demo data sources.",
      }),
    });

    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-orders",
        type: "table",
        name: "SQL result audit-success",
        preview_json: {
          audit_log_id: "audit-success",
          columns: ["order_id", "channel"],
          rows: [["o_001", "search"]],
          row_count: 3,
        },
      },
    });

    expect(run.artifacts[0]?.createdByEventId).toBe("tool-sql-1");
    expect(run.events.find((event) => event.id === "tool-sql-1")?.artifactIds).toEqual([
      "artifact-orders",
    ]);
    expect(run.events.find((event) => event.id === "tool-sql-2")?.artifactIds).toBeUndefined();
    expect(run.events.find((event) => event.id === "tool-sql-2")?.payload).toMatchObject({
      sql: "SELECT channel, COUNT(*) AS count FROM orders GROUP BY channel",
      scannedRows: 0,
    });
  });

  it("stores workspace.metadata and sandbox.output CUSTOM events", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "workspace.metadata",
      value: { toolCallId: "tc-1", toolName: "write_file", status: "ready" },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "sandbox.output",
      value: { kind: "stdout", text: "verify-ok\n" },
    });

    expect(run.workspaceMetadata).toHaveLength(1);
    expect(run.workspaceMetadata[0]).toMatchObject({
      toolCallId: "tc-1",
      toolName: "write_file",
    });
    expect(run.sandboxOutputs).toHaveLength(1);
    expect(run.sandboxOutputs[0]).toMatchObject({ kind: "stdout" });
  });

  it("resolves workspace metadata and sandbox outputs for a tool call", () => {
    let run = createInitialLiveRun();
    run = {
      ...run,
      toolCalls: [
        {
          id: "tc-exec",
          name: "execute_command",
          status: "success",
          startedAtMs: 1000,
          finishedAtMs: 2000,
        },
      ],
      workspaceMetadata: [
        {
          toolCallId: "tc-exec",
          toolName: "execute_command",
          receivedAt: 1500,
          payload: { status: "ready" },
        },
      ],
      sandboxOutputs: [
        {
          kind: "stdout",
          receivedAt: 1600,
          payload: { text: "hello\n" },
        },
      ],
    };

    expect(resolveWorkspaceMetadataForToolCall(run, "tc-exec")?.toolName).toBe(
      "execute_command",
    );
    expect(resolveSandboxOutputsForToolCall(run, run.toolCalls[0])).toHaveLength(1);
    expect(formatSandboxOutputText(run.sandboxOutputs[0]!)).toBe("hello\n");
  });

  it("ignores stale running status snapshots after a completed run", () => {
    let run = reduceLiveRunEvent(createInitialLiveRun(), { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
    expect(run.runStatus).toBe("completed");

    run = reduceLiveRunEvent(run, {
      type: "STATE_SNAPSHOT",
      snapshot: { runStatus: "running" },
    });
    expect(run.runStatus).toBe("completed");

    run = reduceLiveRunEvent(run, {
      type: "STATE_DELTA",
      delta: [{ op: "replace", path: "/runStatus", value: "running" }],
    });
    expect(run.runStatus).toBe("completed");
  });

  it("keeps suspended status when RUN_FINISHED follows ask_user suspension", () => {
    let run = reduceLiveRunEvent(createInitialLiveRun(), { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "STATE_DELTA",
      delta: [{ op: "replace", path: "/runStatus", value: "suspended" }],
    });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });

    expect(run.runStatus).toBe("suspended");
    expect(run.runFinishedAt).toBeTypeOf("number");
  });

  it("ignores stale RUN_ERROR after a completed run", () => {
    let run = reduceLiveRunEvent(createInitialLiveRun(), { type: "RUN_STARTED", runId: "run-1" });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
    expect(run.runStatus).toBe("completed");
    expect(run.errorMessage).toBeUndefined();

    run = reduceLiveRunEvent(run, {
      type: "RUN_ERROR",
      runId: "run-0",
      message: "PROVIDER_CONFIG_MISSING:bad-model",
    });

    expect(run.runStatus).toBe("completed");
    expect(run.errorMessage).toBeUndefined();
  });

  it("clears errorMessage when a run completes successfully", () => {
    let run = reduceLiveRunEvent(createInitialLiveRun(), { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "RUN_ERROR",
      message: "PROVIDER_CONFIG_MISSING:bad-model",
    });
    expect(run.errorMessage).toBeDefined();

    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });

    expect(run.runStatus).toBe("completed");
    expect(run.errorMessage).toBeUndefined();
  });

  it("names collaboration tools from interaction.requested", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "call-ask-1",
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "interaction.requested",
      value: {
        tool_call_id: "call-ask-1",
        tool_name: "ask_user",
      },
    });

    expect(run.toolCalls[0]).toMatchObject({ id: "call-ask-1", name: "ask_user" });
    expect(run.events[0]).toMatchObject({
      id: "call-ask-1",
      toolName: "ask_user",
      title: "Ask user",
    });
  });

  it("replaces existing artifact when same id arrives again (session output upsert)", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-session-1",
        type: "markdown",
        name: "report.md",
        file_id: "file-1",
        download_url: "/api/v1/artifacts/artifact-session-1/download",
        logical_key: "session_file:reports/report.md",
        version: 1,
        created_at: "2026-07-09T10:00:00.000Z",
      },
    });
    expect(run.artifacts).toHaveLength(1);
    expect(run.artifacts[0]).toMatchObject({
      id: "artifact-session-1",
      version: "v1",
      versionCount: 1,
      recordedAtMs: Date.parse("2026-07-09T10:00:00.000Z"),
    });

    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-session-1",
        type: "markdown",
        name: "report.md",
        file_id: "file-2",
        download_url: "/api/v1/artifacts/artifact-session-1/download",
        logical_key: "session_file:reports/report.md",
        version: 2,
        created_at: "2026-07-09T11:00:00.000Z",
      },
    });
    expect(run.artifacts).toHaveLength(1);
    expect(run.artifacts[0]).toMatchObject({
      id: "artifact-session-1",
      logicalKey: "session_file:reports/report.md",
      version: "v2",
      versionCount: 2,
      // First recorded time is preserved so Outputs sort stays stable.
      recordedAtMs: Date.parse("2026-07-09T10:00:00.000Z"),
    });
  });

  it("appends new artifacts in arrival order for oldest-first Outputs", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-a",
        type: "table",
        name: "first",
        created_at: "2026-07-09T09:00:00.000Z",
      },
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-b",
        type: "markdown",
        name: "second",
        created_at: "2026-07-09T10:00:00.000Z",
      },
    });
    expect(run.artifacts.map((artifact) => artifact.id)).toEqual(["artifact-a", "artifact-b"]);
  });

  it("links session-output artifact to write_file tool via tool_call_id", () => {
    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "call-write-1",
      toolCallName: "write_file",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "call-write-1",
      toolCallName: "write_file",
      result: JSON.stringify({ observation: "Wrote report.md" }),
    });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-md-1",
        type: "markdown",
        name: "report.md",
        file_id: "file-1",
        download_url: "/api/v1/artifacts/artifact-md-1/download",
        logical_key: "session_file:reports/report.md",
        version: 1,
        tool_call_id: "call-write-1",
      },
    });
    expect(run.artifacts[0]?.createdByEventId).toBe("call-write-1");
    expect(run.events.find((e) => e.id === "call-write-1")?.artifactIds).toContain("artifact-md-1");
  });
});
