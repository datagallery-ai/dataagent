import { LocalDataGateway } from "../packages/data-gateway/dist/index.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { mkdirSync, writeFileSync } from "node:fs";
import { createServer as createHttpServer } from "node:http";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";
import duckdb from "duckdb";
import writeXlsxFile from "write-excel-file/node";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const stamp = Date.now();
const root = `storage/data-gateway-smoke/${stamp}`;
const metadataPath = `${root}/metadata.sqlite`;
const sqlitePath = `${root}/orders.sqlite`;
const duckdbPath = `${root}/orders.duckdb`;
const csvPath = `${root}/orders.csv`;
const xlsxPath = `${root}/orders.xlsx`;

mkdirSync(root, { recursive: true });
createSqliteFixture(sqlitePath);
await createDuckDbFixture(duckdbPath);
writeFileSync(
  csvPath,
  "order_id,channel,gmv\nc_001,search,1280\nc_002,social,640\nc_003,direct,920\n",
  "utf8"
);
await createXlsxFixture(xlsxPath);

const store = createMetadataStore({ database_path: metadataPath });
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;

const gateway = new LocalDataGateway(store);
const user_id = userId;
const clickHouseServer = createClickHouseFixtureServer();
await new Promise((resolve) => clickHouseServer.listen(0, "127.0.0.1", resolve));
const clickHouseAddress = clickHouseServer.address();
assert(clickHouseAddress && typeof clickHouseAddress === "object", "clickhouse fixture should bind to a port");

try {
  await gateway.registerDataSource({
    user_id,
    id: "sqlite-orders",
    name: "SQLite Orders",
    type: "sqlite",
    config: { path: sqlitePath }
  });
  await gateway.registerDataSource({
    user_id,
    id: "duckdb-orders",
    name: "DuckDB Orders",
    type: "duckdb",
    config: { mode: "file", path: duckdbPath }
  });
  await gateway.registerDataSource({
    user_id,
    id: "csv-orders",
    name: "CSV Orders",
    type: "csv",
    config: { file_path: csvPath, table_name: "orders_csv" }
  });
  await gateway.registerDataSource({
    user_id,
    id: "xlsx-orders",
    name: "XLSX Orders",
    type: "xlsx",
    config: { file_path: xlsxPath, table_name: "orders_xlsx" }
  });
  await gateway.registerDataSource({
    user_id,
    id: "clickhouse-orders",
    name: "ClickHouse Orders",
    type: "clickhouse",
    config: {
      host: "127.0.0.1",
      port: clickHouseAddress.port,
      database: "analytics",
      username: "default",
      password: "fixture-password"
    }
  });

  const supportTypes = await gateway.supportTypes();
  assert(supportTypes.some((type) => type.name === "duckdb" && type.enabled), "duckdb support type missing");
  assert(supportTypes.some((type) => type.name === "sqlite" && type.enabled), "sqlite support type missing");
  assert(
    supportTypes.some((type) => type.name === "clickhouse" && type.enabled),
    "clickhouse support type missing"
  );

  const list = await gateway.listDataSources({ user_id });
  assert(list.length === 5, `expected 5 data sources, got ${list.length}`);
  assert(!JSON.stringify(list).includes("file_path"), "data source list leaked config file_path");

  for (const datasource_id of [
    "duckdb-orders",
    "sqlite-orders",
    "csv-orders",
    "xlsx-orders",
    "clickhouse-orders"
  ]) {
    const test = await gateway.testConnect({ user_id, datasource_id });
    assert(test.ok, `${datasource_id} test-connect failed`);
  }

  const realDuckdbSchema = await gateway.inspectSchema({ user_id, datasource_id: "duckdb-orders" });
  const sqliteSchema = await gateway.inspectSchema({ user_id, datasource_id: "sqlite-orders" });
  const csvPreview = await gateway.previewTable({
    user_id,
    datasource_id: "csv-orders",
    table: "orders_csv",
    limit: 20
  });
  const xlsxPreview = await gateway.previewTable({
    user_id,
    datasource_id: "xlsx-orders",
    table: "orders_xlsx",
    limit: 20
  });
  const clickHouseSchema = await gateway.inspectSchema({ user_id, datasource_id: "clickhouse-orders" });
  const clickHousePreview = await gateway.previewTable({
    user_id,
    datasource_id: "clickhouse-orders",
    table: "orders",
    limit: 20
  });
  const clickHouseSql = await gateway.runSqlReadonly({
    user_id,
    datasource_id: "clickhouse-orders",
    sql: "SELECT channel, gmv FROM orders",
    limit: 20
  });

  assert(realDuckdbSchema.tables.some((table) => table.name === "orders"), "real duckdb orders schema missing");
  assert(sqliteSchema.tables.some((table) => table.name === "orders"), "sqlite orders schema missing");
  assert(clickHouseSchema.tables.some((table) => table.name === "orders"), "clickhouse orders schema missing");
  assert(csvPreview.row_count === 3, `expected 3 CSV rows, got ${csvPreview.row_count}`);
  assert(xlsxPreview.row_count === 3, `expected 3 XLSX rows, got ${xlsxPreview.row_count}`);
  assert(clickHousePreview.row_count === 2, `expected 2 ClickHouse preview rows, got ${clickHousePreview.row_count}`);
  assert(clickHouseSql.row_count === 2, `expected 2 ClickHouse SQL rows, got ${clickHouseSql.row_count}`);

  console.log(
    `Data Gateway smoke OK: sources=${list.length}, ` +
      `duckdb_tables=${realDuckdbSchema.tables.length}, ` +
      `sqlite_tables=${sqliteSchema.tables.length}, csv_rows=${csvPreview.row_count}, ` +
      `xlsx_rows=${xlsxPreview.row_count}, clickhouse_rows=${clickHouseSql.row_count}`
  );
} finally {
  await closeHttpServer(clickHouseServer);
  store.close();
}

function createClickHouseFixtureServer() {
  return createHttpServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) {
      chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    }
    const body = Buffer.concat(chunks).toString("utf8");
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    assert(url.searchParams.get("database") === "analytics", "clickhouse database param missing");
    assert(request.headers["x-clickhouse-user"] === "default", "clickhouse user header missing");
    assert(request.headers["x-clickhouse-key"] === "fixture-password", "clickhouse password header missing");
    response.writeHead(200, { "Content-Type": "application/json" });
    if (body.includes("system.columns")) {
      response.end(JSON.stringify({
        data: [
          { table_name: "orders", column_name: "order_id", data_type: "String", is_nullable: "NO" },
          { table_name: "orders", column_name: "channel", data_type: "String", is_nullable: "NO" },
          { table_name: "orders", column_name: "gmv", data_type: "Float64", is_nullable: "NO" }
        ]
      }));
      return;
    }
    assert(body.includes("FORMAT JSON"), "clickhouse adapter should request JSON results");
    response.end(JSON.stringify({
      data: [
        { order_id: "ch_001", channel: "search", gmv: 1280 },
        { order_id: "ch_002", channel: "social", gmv: 640 }
      ]
    }));
  });
}

async function closeHttpServer(httpServer) {
  await new Promise((resolve, reject) => {
    httpServer.close((error) => error ? reject(error) : resolve());
    setImmediate(() => httpServer.closeAllConnections?.());
  });
  httpServer.unref?.();
}

function createSqliteFixture(path) {
  mkdirSync(dirname(path), { recursive: true });
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

async function createDuckDbFixture(path) {
  mkdirSync(dirname(path), { recursive: true });
  const db = new duckdb.Database(path);
  const connection = db.connect();

  try {
    await duckDbExec(connection, `
      CREATE TABLE orders (
        order_id VARCHAR,
        channel VARCHAR,
        gmv DOUBLE
      );
      INSERT INTO orders VALUES
        ('d_001', 'search', 1280),
        ('d_002', 'social', 640),
        ('d_003', 'direct', 920);
    `);
  } finally {
    await duckDbClose(connection);
    await duckDbCloseDatabase(db);
  }
}

async function duckDbExec(connection, sql) {
  await new Promise((resolve, reject) => {
    connection.exec(sql, (error) => error ? reject(error) : resolve());
  });
}

async function duckDbClose(connection) {
  await new Promise((resolve, reject) => {
    connection.close((error) => error ? reject(error) : resolve());
  });
}

async function duckDbCloseDatabase(db) {
  await new Promise((resolve, reject) => {
    db.close((error) => error ? reject(error) : resolve());
  });
}

async function createXlsxFixture(path) {
  const file = await writeXlsxFile(
    [
      [
        { value: "order_id" },
        { value: "channel" },
        { value: "gmv" }
      ],
      [
        { value: "x_001" },
        { value: "search" },
        { value: 1280 }
      ],
      [
        { value: "x_002" },
        { value: "social" },
        { value: 640 }
      ],
      [
        { value: "x_003" },
        { value: "direct" },
        { value: 920 }
      ]
    ]
  );
  await file.toFile(path);
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}
