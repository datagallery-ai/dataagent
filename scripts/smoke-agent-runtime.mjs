import {
  createDataFoundryRunContext,
  createDataFoundryToolRegistry,
  createToolObservationBoundary,
  GovernedToolFactory,
  SchemaToolObservationAdapter,
  ToolObservationDispatcher,
  toolObservationModelFromPackage
} from "../packages/agent-runtime/dist/testing.js";
import { LocalDataGateway } from "../packages/data-gateway/dist/index.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { EventType } from "@ag-ui/core";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const stamp = Date.now();
const metadataPath = `storage/agent-smoke/${stamp}/metadata.sqlite`;
const sqlitePath = `storage/agent-smoke/${stamp}/large.sqlite`;
const store = createMetadataStore({ database_path: metadataPath });
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;

const gateway = new LocalDataGateway(store);

const user_id = userId;
const session_id = "agent-smoke-session";
const run_id = "agent-smoke-run";
const datasource_id = "agent-sqlite-large";

try {
  createLargeSqliteFixture(sqlitePath);
  await gateway.registerDataSource({
    user_id,
    id: datasource_id,
    name: "Agent SQLite Large",
    type: "sqlite",
    config: { path: sqlitePath }
  });
  store.sessions.create({
    user_id,
    id: session_id,
    title: "Agent smoke",
    selected_datasource_id: datasource_id
  });
  store.runs.create({
    user_id,
    id: run_id,
    session_id,
    user_input: "分析 orders 表的 GMV",
    status: "running",
    datasource_id
  });

  let seq = 0;
  const events = [];
  const runContext = createDataFoundryRunContext({
    user_id,
    session_id,
    run_id,
    user_input: "分析 orders 表的 GMV",
    chat_mode: "chat_data",
    selected_datasource_id: datasource_id,
    enabled_datasource_ids: [datasource_id]
  });
  const runScope = {
    modelName: runContext.model_name,
    resourceId: runContext.user_id,
    runId: runContext.run_id,
    sessionId: runContext.session_id
  };

  const { packager } = createToolObservationBoundary({
    identity: {
      resourceId: runContext.user_id,
      runId: runContext.run_id,
      sessionId: runContext.session_id
    }
  });

  const registry = createDataFoundryToolRegistry({
    dataGateway: gateway,
    runContext,
    emitter: {
      emit: (event) => {
        seq += 1;
        events.push({
          event,
          run_id,
          session_id,
          seq,
          ts: new Date().toISOString()
        });
      }
    }
  });
  let sqlPkg;
  const factory = new GovernedToolFactory(new ToolObservationDispatcher(packager, runScope), (result) => {
    registry.onGovernedResult(result);
    if (result.toolName === "run_sql_readonly") {
      sqlPkg = result.contextPackage;
    }
  });
  const inspectSchemaTool = factory.governTool("inspect_schema", registry.mastraTools.inspect_schema);
  const runSqlTool = factory.governTool("run_sql_readonly", registry.mastraTools.run_sql_readonly);

  await assertRejects(
    () =>
      runSqlTool.execute({
        schema_id: "ds:never-issued",
        sql: "SELECT id, note FROM big_orders",
        limit: 20
      }, {}),
    "SCHEMA_REQUIRED_BEFORE_SQL"
  );

  const schema = await inspectSchemaTool.execute({}, {});
  const schemaId = schema.schema_id;
  const table = schema.tables[0];
  const selectedColumns = table?.columns.slice(0, 3).map((column) => column.name) ?? ["*"];
  const sql = `SELECT ${selectedColumns.join(", ")} FROM ${table?.name ?? "big_orders"} ORDER BY id`;
  const sqlResult = await runSqlTool.execute({ schema_id: schemaId, sql, limit: 100 }, {});

  const schemaActivity = events.find(
    (record) =>
      record.event.type === EventType.ACTIVITY_SNAPSHOT && record.event.content?.tool_name === "inspect_schema"
  );
  const sqlActivity = events.find(
    (record) =>
      record.event.type === EventType.ACTIVITY_SNAPSHOT && record.event.content?.tool_name === "run_sql_readonly"
  );
  const sqlMeta = events.find(
    (record) => record.event.type === EventType.ACTIVITY_SNAPSHOT && typeof record.event.content?.sql === "string"
  );
  const tableOutput = events.find(
    (record) => record.event.type === EventType.ACTIVITY_SNAPSHOT && record.event.content?.output_type === "table"
  );
  const sqlAudit = events.find(
    (record) => record.event.type === EventType.CUSTOM && record.event.name === "sql_audit"
  );
  const artifactEvent = events.find(
    (record) => record.event.type === EventType.CUSTOM && record.event.name === "artifact"
  );
  const legacyEvent = events.find((record) => String(record.event.type).includes("."));
  const auditLogs = store.sqlAuditLogs.listByDataSource({ user_id, datasource_id });

  assert(Boolean(schemaActivity), "inspect_schema activity was not emitted");
  assert(Boolean(sqlActivity), "run_sql_readonly activity was not emitted");
  assert(Boolean(sqlMeta), "SQL activity metadata was not emitted");
  assert(Boolean(tableOutput), "table activity output was not emitted");
  assert(Boolean(sqlAudit), "SQL audit custom event was not emitted");
  assert(Boolean(artifactEvent), "artifact custom event was not emitted");
  assert(
    !("preview_json" in artifactEvent.event.value),
    "artifact event must be a slim reference; preview data should stay behind REST"
  );
  assert(
    artifactEvent.event.value.preview_available === true,
    "artifact event should advertise preview availability"
  );
  assert(
    artifactEvent.event.value.summary.includes("100"),
    "artifact event summary should preserve the stored SQL row count"
  );
  assert(sqlResult.rows.length === 20, `model-visible SQL rows should be 20, got ${sqlResult.rows.length}`);
  assert(sqlResult.row_count === 100, `SQL row_count should preserve original count, got ${sqlResult.row_count}`);
  assert(
    sqlResult.context?.truncation.truncated === true,
    "model-visible SQL result should include truncation metadata"
  );
  assert(Boolean(sqlResult.artifact_id), "SQL result should keep artifact reference after truncation");
  assert(!("artifact" in sqlResult), "model-visible SQL result must not contain artifact preview data");
  assert(sqlPkg.artifactRefs.length === 1, "SQL context package should expose one artifact reference");
  assert(sqlPkg.auditRefs.length === 1, "SQL context package should expose one audit reference");
  assert(
    sqlPkg.truncation.some((entry) => entry.reason.includes("Truncated") && entry.reason.includes("cell")),
    "SQL context package should report cell truncation"
  );
  assert(schemaActivity.event.replace === true, "schema activity should replace previous snapshots");
  assert(sqlActivity.event.replace === true, "SQL activity should replace previous snapshots");
  assert(tableOutput.event.content.content.rows.length === 20, "activity table preview should be truncated to 20 rows");
  assert(
    tableOutput.event.content.content.context?.truncation.truncated === true,
    "activity output truncation metadata missing"
  );
  assert(
    String(tableOutput.event.content.content.rows[0][2]).length <= 600,
    "activity output should truncate long string cells"
  );
  assert(
    schemaActivity.event.messageId !== sqlActivity.event.messageId,
    "STEP activity messageId should include step_id"
  );
  assert(
    !events.some((record) => record.event.activityType === "PLAN"),
    "data tools must not emit a second, fixed PLAN state"
  );
  assert(!legacyEvent, "tool registry should not emit legacy custom event types");
  assert(auditLogs.some((log) => log.status === "succeeded"), "successful SQL audit log missing");

  assertThrows(
    () => packager.packageToolObservation({
      toolName: "unregistered_tool",
      rawResult: { secret: "value" },
      runScope
    }),
    "CONTEXT_ADAPTER_REQUIRED:unregistered_tool"
  );
  assertThrows(
    () =>
      createToolObservationBoundary({
        additionalAdapters: [new SchemaToolObservationAdapter()],
        identity: {
          resourceId: runContext.user_id,
          runId: `${runContext.run_id}-duplicate-adapter`,
          sessionId: runContext.session_id
        }
      }),
    "TOOL_OBSERVATION_ADAPTER_ALREADY_REGISTERED:inspect_schema"
  );

  const oversizedBoundary = createToolObservationBoundary({
    additionalAdapters: [{
      toolName: "oversized_tool",
      resultType: "oversized",
      sourceType: "tool-observation",
      toContextItems: () => [{
        id: "oversized-model",
        sourceType: "tool-observation",
        visibility: "model",
        priority: 1,
        content: "x".repeat(70000),
        metadata: {}
      }]
    }],
    identity: {
      resourceId: runContext.user_id,
      runId: `${runContext.run_id}-oversized`,
      sessionId: runContext.session_id
    }
  });
  assertThrows(
    () => oversizedBoundary.packager.packageToolObservation({ toolName: "oversized_tool", rawResult: {}, runScope }),
    "CONTEXT_CHAR_BUDGET_EXCEEDED:model"
  );

  const wideSqlPackage = packager.packageToolObservation({
    toolName: "run_sql_readonly",
    rawResult: {
      result: {
        columns: Array.from({ length: 80 }, (_, index) => `column_${index}`),
        rows: Array.from({ length: 20 }, () => Array.from({ length: 80 }, () => "z".repeat(500))),
        row_count: 20,
        audit_log_id: "wide-audit",
        elapsed_ms: 1
      },
      sql: "SELECT * FROM wide_table"
    },
    runScope
  });
  const wideSqlModel = toolObservationModelFromPackage(wideSqlPackage);
  assert(JSON.stringify(wideSqlModel).length <= 32000, "wide SQL model result should fit the hard budget");
  assert(wideSqlModel.rows.length < 20, "wide SQL model result should reduce rows to fit the hard budget");

  const wideSchemaPackage = packager.packageToolObservation({
    toolName: "inspect_schema",
    rawResult: {
      datasource_id,
      tables: Array.from({ length: 20 }, (_, tableIndex) => ({
        name: `table_${tableIndex}_${"t".repeat(1000)}`,
        columns: Array.from({ length: 50 }, (_, columnIndex) => ({
          name: `column_${columnIndex}_${"c".repeat(1000)}`,
          type: "TEXT"
        }))
      }))
    },
    runScope
  });
  const wideSchemaModel = toolObservationModelFromPackage(wideSchemaPackage);
  assert(JSON.stringify(wideSchemaModel).length <= 32000, "wide schema should fit the hard budget");
  assert(wideSchemaPackage.truncation.length > 0, "wide schema should report truncation");

  console.log(
    `Agent tool smoke OK: events=${events.length}, sql=${sqlMeta.event.content.sql}, ` +
      `model_rows=${sqlResult.rows.length}, row_count=${tableOutput.event.content.content.row_count}, ` +
      `audit_logs=${auditLogs.length}`
  );
} finally {
  store.close();
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

async function assertRejects(fn, expectedMessage) {
  try {
    await fn();
  } catch (error) {
    if (error instanceof Error && error.message === expectedMessage) {
      return;
    }

    throw error;
  }

  throw new Error(`Expected rejection: ${expectedMessage}`);
}

function assertThrows(fn, expectedMessage) {
  try {
    fn();
  } catch (error) {
    if (error instanceof Error && error.message === expectedMessage) {
      return;
    }
    throw error;
  }
  throw new Error(`Expected exception: ${expectedMessage}`);
}

function createLargeSqliteFixture(path) {
  mkdirSync(dirname(path), { recursive: true });
  const db = new DatabaseSync(path);

  try {
    db.exec(`
      CREATE TABLE big_orders (
        id INTEGER PRIMARY KEY,
        channel TEXT NOT NULL,
        note TEXT NOT NULL
      );
    `);
    const insert = db.prepare("INSERT INTO big_orders (id, channel, note) VALUES (?, ?, ?)");

    for (let index = 1; index <= 100; index += 1) {
      insert.run(index, index % 2 === 0 ? "search" : "direct", `note-${index}-` + "x".repeat(700));
    }
  } finally {
    db.close();
  }
}
