import { LocalDataGateway } from "../packages/data-gateway/dist/index.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { mkdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const stamp = Date.now();
const root = `storage/sql-smoke/${stamp}`;
const metadataPath = `${root}/metadata.sqlite`;
const sqlitePath = `${root}/orders.sqlite`;
mkdirSync(root, { recursive: true });
createSqliteFixture(sqlitePath);

const store = createMetadataStore({ database_path: metadataPath });
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;

const gateway = new LocalDataGateway(store);
const user_id = userId;
const session_id = "sql-smoke-session";
const run_id = "sql-smoke-run";

try {
  store.sessions.create({ user_id, id: session_id, title: "SQL smoke" });
  store.runs.create({
    user_id,
    id: run_id,
    session_id,
    user_input: "SQL smoke",
    status: "running"
  });

  await gateway.registerDataSource({
    user_id,
    id: "sqlite-orders",
    name: "SQLite Orders",
    type: "sqlite",
    config: { path: sqlitePath }
  });

  const selectResult = await gateway.runSqlReadonly({
    user_id,
    run_id,
    datasource_id: "sqlite-orders",
    sql: "SELECT order_id, channel, gmv FROM orders",
    limit: 2
  });
  assert(selectResult.row_count === 2, `SELECT row_count expected 2, got ${selectResult.row_count}`);
  assert(Boolean(selectResult.artifact_id), "SELECT should create a table artifact when run_id is provided");

  // R-018: correlation handles land on the artifact's metadata_json for Detail linking.
  const correlatedResult = await gateway.runSqlReadonly({
    user_id,
    run_id,
    datasource_id: "sqlite-orders",
    sql: "SELECT channel, COUNT(*) AS n FROM orders GROUP BY channel",
    limit: 5,
    correlation: { tool_call_id: "call-r018", step_id: "sql-r018" }
  });
  assert(Boolean(correlatedResult.artifact_id), "correlated SELECT should create a table artifact");
  const correlatedArtifact = store.artifacts.get({ user_id, artifact_id: correlatedResult.artifact_id });
  const correlatedMeta = correlatedArtifact.metadata_json ? JSON.parse(correlatedArtifact.metadata_json) : {};
  assert(correlatedMeta.tool_call_id === "call-r018", "artifact metadata should carry tool_call_id");
  assert(correlatedMeta.step_id === "sql-r018", "artifact metadata should carry step_id");
  assert(typeof correlatedMeta.audit_log_id === "string", "artifact metadata should carry audit_log_id");

  const withResult = await gateway.runSqlReadonly({
    user_id,
    datasource_id: "sqlite-orders",
    sql: "WITH recent_orders AS (SELECT order_id, gmv FROM orders) SELECT * FROM recent_orders LIMIT 2"
  });
  assert(withResult.row_count === 2, `WITH row_count expected 2, got ${withResult.row_count}`);

  const commentedResult = await gateway.runSqlReadonly({
    user_id,
    datasource_id: "sqlite-orders",
    sql: [
      "-- 整体业务概览：GMV、订单量、毛利率、退款率、新客占比",
      "SELECT order_id, channel, gmv FROM orders"
    ].join("\n"),
    limit: 2
  });
  assert(
    commentedResult.row_count === 2,
    `commented SELECT row_count expected 2, got ${commentedResult.row_count}`
  );

  await assertBlocked(() =>
    gateway.runSqlReadonly({
      user_id,
      datasource_id: "sqlite-orders",
      sql: "-- looks safe\nDROP TABLE orders"
    })
  );

  for (const sql of [
    "DELETE FROM orders",
    "DROP TABLE orders",
    "UPDATE orders SET gmv = 0",
    "ALTER TABLE orders ADD COLUMN x INT",
    "TRUNCATE TABLE orders",
    "CREATE TABLE copied AS SELECT * FROM orders",
    "SELECT * FROM orders; DROP TABLE orders;"
  ]) {
    await assertBlocked(() =>
      gateway.runSqlReadonly({
        user_id,
        datasource_id: "sqlite-orders",
        sql
      })
    );
  }

  const auditLogs = store.sqlAuditLogs.listByDataSource({ user_id, datasource_id: "sqlite-orders" });
  const succeededLogs = auditLogs.filter((log) => log.status === "succeeded");
  const blockedLogs = auditLogs.filter((log) => log.status === "blocked");
  const artifacts = store.artifacts.listByRun({ user_id, run_id });

  assert(succeededLogs.length >= 3, `expected at least 3 succeeded audit logs, got ${succeededLogs.length}`);
  assert(blockedLogs.length === 8, `expected 8 blocked audit logs, got ${blockedLogs.length}`);
  assert(artifacts.some((artifact) => artifact.type === "table"), "expected a table artifact for SELECT result");

  console.log(
    `SQL smoke OK: select_rows=${selectResult.row_count}, with_rows=${withResult.row_count}, ` +
      `blocked=${blockedLogs.length}, audit_logs=${auditLogs.length}, artifacts=${artifacts.length}`
  );
} finally {
  store.close();
}

function createSqliteFixture(path) {
  const db = new DatabaseSync(path);

  try {
    db.exec(`
      CREATE TABLE orders (
        order_id TEXT PRIMARY KEY,
        channel TEXT NOT NULL,
        gmv REAL NOT NULL
      );
      INSERT INTO orders (order_id, channel, gmv) VALUES
        ('s_001', 'search', 1280),
        ('s_002', 'social', 640),
        ('s_003', 'direct', 920);
    `);
  } finally {
    db.close();
  }
}

async function assertBlocked(callback) {
  try {
    await callback();
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("SQL_BLOCKED:")) {
      return;
    }

    throw error;
  }

  throw new Error("Expected SQL to be blocked");
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}
