import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createServer as createApiServer } from "../apps/api/dist/server.js";
import { createTaskStateRuntime } from "../packages/agent-runtime/dist/index.js";
import { LocalDataGateway } from "../packages/data-gateway/dist/index.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { createAuthenticatedTestClient } from "./lib/authenticated-test-client.mjs";

const root = mkdtempSync(join(tmpdir(), "open-data-foundry-server-datasources-e2e-"));
process.env.DATAFOUNDRY_AUTH_MODE = "password";
process.env.AUTH_SESSION_SECRET = "server-datasources-e2e-session-secret-32b!";
process.env.AUTH_PUBLIC_BASE_URL = "http://127.0.0.1:3000";
process.env.AUTH_EMAIL_DELIVERY = "test";
process.env.AUTH_REGISTRATION_MODE = "open";
process.env.STORAGE_ROOT_DIR = root;
process.env.MASTRA_STORAGE_PATH = join(root, "mastra.sqlite");
process.env.EMBEDDING_API_KEY = "";

const targets = [
  serverTarget("postgresql", "PG", {
    schema: env("ODA_E2E_PG_SCHEMA") ?? "public",
    sql: env("ODA_E2E_PG_SQL") ?? "SELECT 1 AS value"
  }),
  serverTarget("mysql", "MYSQL", {
    sql: env("ODA_E2E_MYSQL_SQL") ?? "SELECT 1 AS value"
  }),
  serverTarget("clickhouse", "CLICKHOUSE", {
    secure: booleanEnv("ODA_E2E_CLICKHOUSE_SECURE"),
    sql: env("ODA_E2E_CLICKHOUSE_SQL") ?? "SELECT 1 AS value"
  })
].filter((target) => target.enabled);

if (targets.length === 0) {
  console.log(
    "Server datasource E2E skipped: set ODA_E2E_PG_*, ODA_E2E_MYSQL_*, or ODA_E2E_CLICKHOUSE_* env vars."
  );
  process.exit(0);
}

const metadataStore = createMetadataStore({
  database_path: join(root, "metadata.sqlite"),
  secret_master_key: "server-datasources-e2e-master-key"
});
const taskStateRuntime = await createTaskStateRuntime(join(root, "task-state.sqlite"));
const dataGateway = new LocalDataGateway(metadataStore);
const server = await createApiServer({ metadataStore, taskStateRuntime });
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const address = server.address();
assert(address && typeof address === "object");
const baseUrl = `http://127.0.0.1:${address.port}`;
const client = createAuthenticatedTestClient({ baseUrl });
const identity = await client.registerAndLogin({ displayName: "Server Datasources E2E" });
const { userId, workspaceId } = identity;

try {
  for (const target of targets) {
    await verifyTarget(target);
  }
  console.log(`Server datasource E2E OK: ${targets.map((target) => target.type).join(", ")}`);
} finally {
  await closeHttpServer(server);
  await taskStateRuntime.close();
}

async function verifyTarget(target) {
  const datasourceId = `e2e-${target.type}`;
  const created = await requestJson("/api/v1/datasources", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: datasourceId,
      name: `E2E ${target.type}`,
      type: target.type,
      settings: target.settings,
      credentials: target.credentials
    })
  });
  assert.equal(created.response.status, 201, JSON.stringify(created.body));
  assert.equal(created.body.data.hasSecret, target.hasSecret);

  const test = await requestJson(`/api/v1/datasources/${datasourceId}/test`, { method: "POST" });
  assert.equal(test.response.status, 200, JSON.stringify(test.body));
  assert.equal(test.body.data.ok, true);

  const introspect = await requestJson(`/api/v1/datasources/${datasourceId}/introspect`, { method: "POST" });
  assert.equal(introspect.response.status, 202, JSON.stringify(introspect.body));
  assert.equal(introspect.body.data.status, "completed");

  const schemaResponse = await requestJson(`/api/v1/datasources/${datasourceId}/schema`);
  assert.equal(schemaResponse.response.status, 200, JSON.stringify(schemaResponse.body));
  assert(Array.isArray(schemaResponse.body.data.tables), `${target.type} schema tables missing`);

  const schema = await dataGateway.inspectSchema({ user_id: userId, workspace_id: workspaceId, datasource_id: datasourceId });
  assert(Array.isArray(schema.tables), `${target.type} inspectSchema failed`);

  const result = await dataGateway.runSqlReadonly({
    user_id: userId,
    workspace_id: workspaceId,
    datasource_id: datasourceId,
    sql: target.sql,
    limit: 10
  });
  assert(result.row_count >= 1, `${target.type} readonly SQL returned no rows`);
}

function serverTarget(type, prefix, overrides = {}) {
  const host = env(`ODA_E2E_${prefix}_HOST`);
  const database = env(`ODA_E2E_${prefix}_DATABASE`);
  const username = env(`ODA_E2E_${prefix}_USERNAME`);
  const password = env(`ODA_E2E_${prefix}_PASSWORD`);
  const port = numberEnv(`ODA_E2E_${prefix}_PORT`);
  if (!host || !database || (type !== "clickhouse" && !username)) {
    return { enabled: false, type };
  }
  return {
    credentials: password ? { password } : {},
    enabled: true,
    hasSecret: Boolean(password),
    settings: {
      host,
      database,
      ...(port ? { port } : {}),
      ...(username ? { username } : {}),
      ...(overrides.schema ? { schema: overrides.schema } : {}),
      ...(overrides.secure !== undefined ? { secure: overrides.secure } : {})
    },
    sql: overrides.sql,
    type
  };
}

async function requestJson(path, init = {}) {
  const response = await client.fetch(path, init);
  const body = await response.json();
  return { body, response };
}

function env(name) {
  const value = process.env[name]?.trim();
  return value || undefined;
}

function numberEnv(name) {
  const value = env(name);
  if (!value) {
    return undefined;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function booleanEnv(name) {
  const value = env(name);
  if (!value) {
    return undefined;
  }
  return value === "true" || value === "1";
}

async function closeHttpServer(httpServer) {
  await new Promise((resolve, reject) => {
    httpServer.close((error) => error ? reject(error) : resolve());
    setImmediate(() => httpServer.closeAllConnections?.());
  });
  httpServer.unref?.();
}
