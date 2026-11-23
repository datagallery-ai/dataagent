import type { LiveRun } from "./live-run-state";
import { createInitialLiveRun, reduceLiveRunEvent } from "./live-run-state";

export const toolParametersFixtures = {
  list_data_sources: { enabled_only: true },
  inspect_schema: { datasource_id: "api-duckdb-demo", table_names: ["orders"] },
  preview_table: { schema_id: "schema_test", table: "orders", limit: 5 },
  run_sql_readonly: { sql: "SELECT id, total FROM orders LIMIT 5", limit: 5 },
  retrieve_knowledge: { collection_id: "kb-main", query: "refund policy", top_k: 3 },
  read_file: { path: "reports/summary.md" },
  write_file: { path: "reports/summary.md", content: "# Summary" },
  edit_file: { path: "reports/summary.md", old_string: "draft", new_string: "final" },
  list_files: { path: "reports" },
  grep: { pattern: "total", path: "reports" },
  file_stat: { path: "reports/summary.md" },
  mkdir: { path: "reports/archive" },
  execute_command: { command: "echo verify-ok" },
  task_write: {
    tasks: [{ id: "step1", content: "Inspect schema", status: "in_progress" }],
  },
  ask_user: { question: "Which datasource should I use?" },
  submit_plan: { plan: "1. Inspect schema\n2. Run SQL" },
} as const;

export const toolResultFixtures = {
  list_data_sources: {
    direct: {
      datasources: [
        {
          id: "api-duckdb-demo",
          name: "API DuckDB Demo",
          type: "duckdb",
          status: "ready",
        },
      ],
    },
    observationWrapped: {
      observation: JSON.stringify({
        datasources: [
          {
            id: "api-duckdb-demo",
            name: "API DuckDB Demo",
            type: "duckdb",
            status: "ready",
          },
        ],
      }),
    },
  },
  inspect_schema: {
    direct: {
      datasource_id: "api-duckdb-demo",
      schema_id: "schema_abc",
      tables: [
        {
          name: "orders",
          columns: [
            { name: "id", type: "INTEGER", nullable: false },
            { name: "total", type: "DOUBLE", nullable: true },
          ],
        },
      ],
    },
    observationWrapped: {
      observation: JSON.stringify({
        datasource_id: "api-duckdb-demo",
        tables: [{ name: "orders", columns: [{ name: "id", type: "INTEGER" }] }],
      }),
    },
  },
  preview_table: {
    direct: {
      table: "orders",
      row_count: 2,
      columns: ["id", "total"],
      rows: [
        [1, 42.5],
        [2, 18],
      ],
    },
  },
  run_sql_readonly: {
    flat: {
      columns: ["total_orders"],
      rows: [[128]],
      row_count: 1,
      elapsed_ms: 24,
      audit_log_id: "audit-1",
    },
    nested: {
      sql: "SELECT COUNT(*) AS total_orders FROM orders",
      result: {
        columns: ["total_orders"],
        rows: [[128]],
        row_count: 1,
        elapsed_ms: 24,
        audit_log_id: "audit-1",
      },
    },
    observationWrapped: {
      observation: JSON.stringify({
        columns: ["total_orders"],
        rows: [[128]],
        row_count: 1,
      }),
    },
    emptyRows: {
      columns: ["total_orders"],
      rows: [],
      row_count: 0,
    },
  },
  retrieve_knowledge: {
    direct: {
      collection_id: "kb-main",
      chunks: [
        {
          document_id: "doc-1",
          text: "Refunds are processed within 7 days.",
          source: "policy.md",
        },
      ],
    },
  },
  read_file: "1| # Summary\n2| Revenue increased.\n",
  write_file: "Wrote 128 bytes to reports/summary.md",
  edit_file: "Replaced 1 occurrence in reports/summary.md (lines 3-3)",
  list_files: "reports/\n  summary.md\n  archive/",
  grep: "2 matches in reports/\nreports/summary.md:12:total revenue\n---",
  file_stat: "reports/summary.md Type: file Size: 128 bytes Modified: 2026-07-06T04:00:00Z",
  mkdir: "Created directory reports/archive",
  execute_command: "verify-ok\n",
  execute_command_empty: "",
  task_write: {
    content: "Created 1 task.",
    tasks: [{ id: "step1", content: "Inspect schema", status: "in_progress" }],
    summary: { total: 1, completed: 0, allCompleted: false },
  },
  ask_user: { content: "Waiting for the user to choose a datasource." },
  submit_plan: { content: "Plan submitted for review." },
  protocolError: {
    status: "error",
    reason: "missing_terminal_event",
    message: "Cannot send event type 'TOOL_CALL_RESULT'",
  },
  toolNotDelivered: {
    error: "TOOL_RESULT_NOT_DELIVERED",
    message: "The backend did not push TOOL_CALL_RESULT in the AG-UI stream.",
  },
  preview_table_object_rows: {
    table: "orders",
    row_count: 1,
    columns: ["id", "total"],
    rows: [{ id: 1, total: 42.5 }],
  },
} as const;

export function buildLiveRunWithParallelTools(): LiveRun {
  let run = reduceLiveRunEvent(createInitialLiveRun(), { type: "RUN_STARTED" });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_START",
    toolCallId: "tc-list",
    toolCallName: "list_data_sources",
  });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_RESULT",
    toolCallId: "tc-list",
    toolCallName: "list_data_sources",
    result: JSON.stringify(toolResultFixtures.list_data_sources.direct),
  });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_START",
    toolCallId: "tc-schema",
    toolCallName: "inspect_schema",
  });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_RESULT",
    toolCallId: "tc-schema",
    toolCallName: "inspect_schema",
    result: JSON.stringify(toolResultFixtures.inspect_schema.direct),
  });
  run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
  return run;
}

export function buildLiveRunWithToolSequence(): LiveRun {
  let run = reduceLiveRunEvent(createInitialLiveRun(), { type: "RUN_STARTED" });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_START",
    toolCallId: "tc-list",
    toolCallName: "list_data_sources",
  });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_RESULT",
    toolCallId: "tc-list",
    toolCallName: "list_data_sources",
    result: JSON.stringify(toolResultFixtures.list_data_sources.direct),
  });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_START",
    toolCallId: "tc-schema",
    toolCallName: "inspect_schema",
  });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_RESULT",
    toolCallId: "tc-schema",
    toolCallName: "inspect_schema",
    result: JSON.stringify(toolResultFixtures.inspect_schema.direct),
  });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_START",
    toolCallId: "tc-sql",
    toolCallName: "run_sql_readonly",
    args: { sql: toolParametersFixtures.run_sql_readonly.sql },
  });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_RESULT",
    toolCallId: "tc-sql",
    toolCallName: "run_sql_readonly",
    result: JSON.stringify(toolResultFixtures.run_sql_readonly.flat),
  });
  run = reduceLiveRunEvent(run, {
    type: "CUSTOM",
    name: "workspace.metadata",
    value: {
      toolCallId: "tc-write",
      path: "reports/summary.md",
      operation: "write_file",
    },
  });
  run = reduceLiveRunEvent(run, {
    type: "CUSTOM",
    name: "sandbox.output",
    value: {
      kind: "stdout",
      text: "verify-ok\n",
    },
  });
  run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
  return run;
}

export function buildLiveRunWithNestedSqlResult(): LiveRun {
  let run = reduceLiveRunEvent(createInitialLiveRun(), { type: "RUN_STARTED" });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_START",
    toolCallId: "tc-sql-nested",
    toolCallName: "run_sql_readonly",
    args: { sql: "SELECT COUNT(*) AS total_orders FROM orders" },
  });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_RESULT",
    toolCallId: "tc-sql-nested",
    toolCallName: "run_sql_readonly",
    result: JSON.stringify(toolResultFixtures.run_sql_readonly.nested),
  });
  run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
  return run;
}

export function buildLiveRunWithObservationWrappedSchema(): LiveRun {
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
    result: JSON.stringify({
      observation: JSON.stringify(toolResultFixtures.inspect_schema.direct),
    }),
  });
  run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
  return run;
}

export const assistantMessagesWithParallelTools = [
  {
    id: "assistant-parallel",
    role: "assistant",
    content: "I'll inspect schema and list sources in parallel.",
    toolCalls: [
      { id: "tc-list", function: { name: "list_data_sources" } },
      { id: "tc-schema", function: { name: "inspect_schema" } },
    ],
  },
] as const;

export const assistantMessagesWithTools = [
  {
    id: "assistant-1",
    role: "assistant",
    content: "I'll inspect the datasource first.",
    toolCalls: [{ id: "tc-list", function: { name: "list_data_sources" } }],
  },
  {
    id: "assistant-2",
    role: "assistant",
    content: "Schema looks good; running SQL next.",
    toolCalls: [
      { id: "tc-schema", function: { name: "inspect_schema" } },
      { id: "tc-sql", function: { name: "run_sql_readonly" } },
    ],
  },
] as const;
