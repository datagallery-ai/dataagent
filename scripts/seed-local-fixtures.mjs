#!/usr/bin/env node
/**
 * Create zero-extra-deps local fixtures (CSV + SQLite) and register them via Config API.
 * DuckDB demo (`api-duckdb-demo`) is optional and is no longer auto-seeded by the API server.
 */
import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";
import { createAuthenticatedTestClient } from "./lib/authenticated-test-client.mjs";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const fixturesRoot = resolve(repoRoot, "storage/fixtures");
const csvPath = resolve(fixturesRoot, "orders.csv");
const sqlitePath = resolve(fixturesRoot, "orders.sqlite");

const apiBase =
  process.env.CONFIG_API_URL?.replace(/\/$/u, "") ??
  process.env.NEXT_PUBLIC_CONFIG_API_URL?.replace(/\/$/u, "") ??
  "http://127.0.0.1:8787";

mkdirSync(fixturesRoot, { recursive: true });

writeFileSync(
  csvPath,
  "order_id,channel,gmv\nc_001,search,1280\nc_002,social,640\nc_003,direct,920\n",
  "utf8",
);

const sqlite = new DatabaseSync(sqlitePath);
try {
  sqlite.exec(`
    CREATE TABLE IF NOT EXISTS orders (
      order_id TEXT PRIMARY KEY,
      channel TEXT NOT NULL,
      gmv REAL NOT NULL
    );
    DELETE FROM orders;
    INSERT INTO orders (order_id, channel, gmv) VALUES
      ('s_001', 'search', 1280),
      ('s_002', 'social', 640),
      ('s_003', 'direct', 920);
  `);
} finally {
  sqlite.close();
}

console.log(`[seed] fixtures written to ${fixturesRoot}`);

const datasources = [
  {
    id: "local-csv-orders",
    name: "Local CSV Orders",
    type: "csv",
    config: { file_path: csvPath, table_name: "orders_csv" },
  },
  {
    id: "local-sqlite-orders",
    name: "Local SQLite Orders",
    type: "sqlite",
    settings: { filePath: sqlitePath },
  },
];

const client = createAuthenticatedTestClient({ baseUrl: apiBase });
let authenticated = false;
async function ensureAuth() {
  if (!authenticated) {
    await client.registerAndLogin({ displayName: "Seed Local Fixtures" });
    authenticated = true;
  }
}

async function requestJson(path, init = {}) {
  await ensureAuth();
  const response = await client.fetch(path, init);
  const body = await response.json();
  return { body, response };
}

async function upsertDatasource(spec) {
  const existing = await requestJson(`/api/v1/datasources/${encodeURIComponent(spec.id)}`);
  if (existing.response.status === 200) {
    const updated = await requestJson(`/api/v1/datasources/${encodeURIComponent(spec.id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: spec.name,
        type: spec.type,
        defaultEnabled: true,
        ...(spec.settings ? { settings: spec.settings } : {}),
        ...(spec.config ? { config: spec.config } : {}),
      }),
    });
    assert.equal(
      updated.response.status,
      200,
      `update ${spec.id} failed: ${JSON.stringify(updated.body)}`,
    );
    console.log(`[seed] updated ${spec.id}`);
    return updated.body.data;
  }
  const created = await requestJson("/api/v1/datasources", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: spec.id,
      name: spec.name,
      type: spec.type,
      defaultEnabled: true,
      settings: spec.settings,
      ...(spec.config ? { config: spec.config } : {}),
    }),
  });
  assert.equal(
    created.response.status,
    201,
    `create ${spec.id} failed: ${JSON.stringify(created.body)}`,
  );
  console.log(`[seed] registered ${spec.id}`);
  return created.body.data;
}

async function verifyDatasource(id) {
  const test = await requestJson(`/api/v1/datasources/${encodeURIComponent(id)}/test`, {
    method: "POST",
  });
  assert.equal(test.body.success, true, `${id} test-connect failed: ${JSON.stringify(test.body)}`);

  await requestJson(`/api/v1/datasources/${encodeURIComponent(id)}/introspect`, {
    method: "POST",
    headers: { "Idempotency-Key": `seed-${id}` },
  });

  const schema = await requestJson(`/api/v1/datasources/${encodeURIComponent(id)}/schema`);
  assert.equal(schema.response.status, 200, `${id} schema fetch failed`);
  const tables = schema.body.data?.tables ?? schema.body.data?.schema?.tables ?? [];
  const tableNames = tables.map((table) => table.name);
  console.log(`[seed] ${id}: test OK, tables=${tableNames.join(", ") || "(none)"}`);
}

try {
  const health = await fetch(`${apiBase}/healthz`); // public
  if (!health.ok) {
    throw new Error(`API not reachable at ${apiBase} — start with: npm run dev:api`);
  }

  for (const spec of datasources) {
    await upsertDatasource(spec);
  }

  const demo = await requestJson("/api/v1/datasources/api-duckdb-demo");
  if (demo.response.status !== 200) {
    console.warn("[seed] api-duckdb-demo not found — create it manually if you need the DuckDB demo");
  } else {
    console.log("[seed] api-duckdb-demo present");
  }

  const verifiedIds = [];
  for (const spec of datasources) {
    await verifyDatasource(spec.id);
    verifiedIds.push(spec.id);
  }
  if (demo.response.status === 200) {
    try {
      await verifyDatasource("api-duckdb-demo");
      verifiedIds.push("api-duckdb-demo");
    } catch (error) {
      console.warn(
        `[seed] api-duckdb-demo verification skipped: ${
          error instanceof Error ? error.message : String(error)
        }`,
      );
    }
  }

  console.log(
    `[seed] done — open http://localhost:3000/data-tasks and enable: ${verifiedIds.join(", ")}`,
  );
} catch (error) {
  console.error(`[seed] ${error instanceof Error ? error.message : String(error)}`);
  console.error("[seed] fixture files are still on disk; register them manually in the UI with these paths:");
  console.error(`  CSV:    ${csvPath}`);
  console.error(`  SQLite: ${sqlitePath}`);
  process.exit(1);
}
