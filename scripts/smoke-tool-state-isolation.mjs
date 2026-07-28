import {
  createDataFoundryRunContext,
  createDataFoundryToolRegistry,
  createToolObservationBoundary,
  GovernedToolFactory,
  ToolObservationDispatcher
} from "../packages/agent-runtime/dist/testing.js";
import { LocalDataGateway } from "../packages/data-gateway/dist/index.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { mkdirSync, rmSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const stamp = Date.now();
const storageDir = `storage/tool-state-smoke/${stamp}`;
const metadataPath = `${storageDir}/metadata.sqlite`;
const sqlitePathA = `${storageDir}/orders-a.sqlite`;
const sqlitePathB = `${storageDir}/orders-b.sqlite`;
const store = createMetadataStore({ database_path: metadataPath });
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;

const gateway = new LocalDataGateway(store);

const user_id = userId;
const session_id = "tool-state-session";
const run_id = "tool-state-run";
const datasource_a = "ds-orders-a";
const datasource_b = "ds-orders-b";

try {
  createOrdersFixture(sqlitePathA);
  createOrdersFixture(sqlitePathB);
  await gateway.registerDataSource({
    user_id, id: datasource_a, name: "A", type: "sqlite", config: { path: sqlitePathA }
  });
  await gateway.registerDataSource({
    user_id, id: datasource_b, name: "B", type: "sqlite", config: { path: sqlitePathB }
  });
  store.sessions.create({ user_id, id: session_id, title: "tool-state smoke", selected_datasource_id: datasource_a });
  store.runs.create({
    user_id,
    id: run_id,
    session_id,
    user_input: "tool-state smoke",
    status: "running",
    datasource_id: datasource_a
  });

  const runContext = createDataFoundryRunContext({
    user_id,
    session_id,
    run_id,
    user_input: "tool-state smoke",
    chat_mode: "chat_data",
    selected_datasource_id: datasource_a,
    enabled_datasource_ids: [datasource_a, datasource_b]
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

  const events = [];
  const registry = createDataFoundryToolRegistry({
    dataGateway: gateway,
    runContext,
    emitter: { emit: (event) => events.push(event) }
  });
  const factory = new GovernedToolFactory(
    new ToolObservationDispatcher(packager, runScope),
    registry.onGovernedResult
  );
  const inspectSchema = factory.governTool("inspect_schema", registry.mastraTools.inspect_schema);
  const runSql = factory.governTool("run_sql_readonly", registry.mastraTools.run_sql_readonly);

  // 1. SQL without a schema_id is rejected.
  await assertRejects(
    () => runSql.execute({ schema_id: "never-issued", sql: "SELECT 1" }, {}),
    "SCHEMA_REQUIRED_BEFORE_SQL"
  );

  // 2. inspect_schema issues a schema_id; the same id authorizes repeated queries.
  const inspectedA = await inspectSchema.execute({ datasource_id: datasource_a }, {});
  const schemaIdA = inspectedA.schema_id;
  assert(schemaIdA.startsWith("schema_"), `schema_id should be opaque, got ${schemaIdA}`);

  const first = await runSql.execute({ schema_id: schemaIdA, sql: "SELECT COUNT(*) AS c FROM orders" }, {});
  assert(first.rows[0][0] === 50, "first query on A should see 50 rows");
  const second = await runSql.execute({ schema_id: schemaIdA, sql: "SELECT COUNT(*) AS c FROM orders" }, {});
  assert(second.rows[0][0] === 50, "reused schema_id should still authorize queries");
  assert(
    registry.state.sql_execution_count === 2,
    "run-global SQL count should track reuse of one schema_id"
  );

  // 3. A second datasource gets its own schema_id but shares the run-global SQL budget.
  const inspectedB = await inspectSchema.execute({ datasource_id: datasource_b }, {});
  const schemaIdB = inspectedB.schema_id;
  assert(schemaIdB !== schemaIdA, "different datasources must yield different schema_ids");
  await runSql.execute({ schema_id: schemaIdB, sql: "SELECT COUNT(*) AS c FROM orders" }, {});
  assert(
    registry.state.sql_execution_count === 3,
    "datasource B must consume the same run-global SQL budget"
  );
  assert(
    registry.state.sql_execution_count_by_datasource.get(datasource_a) === 2
      && registry.state.sql_execution_count_by_datasource.get(datasource_b) === 1,
    "per-datasource counts should remain metrics only"
  );

  // 4. Re-inspection issues another opaque token without resetting the global budget.
  mutateOrdersSchema(sqlitePathA);
  const inspectedA2 = await inspectSchema.execute({ datasource_id: datasource_a }, {});
  const schemaIdA2 = inspectedA2.schema_id;
  assert(schemaIdA2 !== schemaIdA, "each completed inspect should issue a fresh opaque schema_id");

  // 5. Concurrent run_sql_readonly on the same schema_id does not lose count increments
  //    and does not produce duplicate stepIds.
  const concurrent = await Promise.all(
    Array.from({ length: 5 }, () =>
      runSql.execute({ schema_id: schemaIdA2, sql: "SELECT 1" }, {})
    )
  );
  assert(concurrent.length === 5, "all concurrent queries should resolve");
  assert(
    registry.state.sql_execution_count === 8,
    `5 concurrent queries should atomically raise the run-global count to 8, got ${registry.state.sql_execution_count}`
  );
  // Each run_sql_readonly call emits two ACTIVITY_SNAPSHOT events (running + completed) sharing
  // one step_id. Under concurrency the counter must still assign distinct sequence numbers with
  // no gaps or collisions, so the set of sequence numbers should be exactly {1..5}.
  const sqlStepIds = events
    .filter((event) => event.type === "ACTIVITY_SNAPSHOT" && event.content?.tool_name === "run_sql_readonly")
    .map((event) => event.content.step_id);
  const recentStepIds = sqlStepIds.filter((id) => /^sql-[4-8]$/.test(id));
  const sequenceNumbers = new Set(
    recentStepIds.map((id) => Number.parseInt(id.slice(id.lastIndexOf("-") + 1), 10))
  );
  assert(
    sequenceNumbers.size === 5 && [...sequenceNumbers].sort((a, b) => a - b).every((n, i) => n === i + 4),
    `concurrent stepId sequence numbers should be exactly 4..8, got ${JSON.stringify([...sequenceNumbers])}`
  );

  console.log(
    `Tool state isolation smoke OK: schema_ids=${registry.state.schema_capabilities.size}, `
      + `concurrent_seq=${JSON.stringify([...sequenceNumbers].sort((a, b) => a - b))}`
  );
} finally {
  rmSync(storageDir, { force: true, recursive: true });
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

async function assertRejects(thunk, expectedMessage) {
  try {
    await thunk();
  } catch (error) {
    if (!(error instanceof Error) || !error.message.includes(expectedMessage)) {
      throw new Error(`Expected rejection with ${expectedMessage}, got ${error?.message ?? error}`);
    }
    return;
  }
  throw new Error(`Expected rejection with ${expectedMessage}, but the call succeeded`);
}

function createOrdersFixture(path) {
  mkdirSync(dirname(path), { recursive: true });
  const db = new DatabaseSync(path);
  try {
    db.exec(`
      CREATE TABLE orders (
        id INTEGER PRIMARY KEY,
        channel TEXT NOT NULL,
        amount REAL NOT NULL
      );
    `);
    const insert = db.prepare("INSERT INTO orders (id, channel, amount) VALUES (?, ?, ?)");
    for (let index = 1; index <= 50; index += 1) {
      insert.run(index, index % 2 === 0 ? "search" : "direct", index * 10);
    }
  } finally {
    db.close();
  }
}

function mutateOrdersSchema(path) {
  const db = new DatabaseSync(path);
  try {
    db.exec("ALTER TABLE orders ADD COLUMN status TEXT NOT NULL DEFAULT 'open'");
  } finally {
    db.close();
  }
}
