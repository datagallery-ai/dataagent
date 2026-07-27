import assert from "node:assert/strict";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  DTC_GROWTH_DATASOURCE_ID,
  DTC_GROWTH_DATASOURCE_NAME,
  ensureBuiltinDtcGrowthDatasource,
  resolveDtcGrowthFixturePath
} from "../apps/api/dist/builtin-dtc-growth-datasource.js";
import { createServer as createApiServer } from "../apps/api/dist/server.js";
import { LocalDataGateway } from "../packages/data-gateway/dist/index.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { createAuthenticatedTestClient } from "./lib/authenticated-test-client.mjs";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoFixture = join(repoRoot, "storage/fixtures/dtc-growth-demo.sqlite");

test("resolveDtcGrowthFixturePath prefers env override", () => {
  const previous = process.env.DTC_GROWTH_FIXTURE_PATH;
  process.env.DTC_GROWTH_FIXTURE_PATH = "/tmp/custom-dtc.sqlite";
  try {
    assert.equal(resolveDtcGrowthFixturePath(), resolve("/tmp/custom-dtc.sqlite"));
  } finally {
    if (previous === undefined) {
      delete process.env.DTC_GROWTH_FIXTURE_PATH;
    } else {
      process.env.DTC_GROWTH_FIXTURE_PATH = previous;
    }
  }
});

test("ensureBuiltinDtcGrowthDatasource copies fixture and registers datasource idempotently", () => {
  assert.ok(existsSync(repoFixture), `fixture missing: ${repoFixture}`);
  const root = mkdtempSync(join(tmpdir(), "dtc-growth-builtin-"));
  const workspaceRoot = join(root, "workspaces");
  const metadataStore = createMetadataStore({
    database_path: join(root, "metadata.sqlite"),
    secret_master_key: "dtc-growth-builtin-test-key"
  });
  metadataStore.users.upsertDevUser({
    id: "user-a",
    email: "user-a@example.com",
    display_name: "User A",
    dev_token: "user-a-token"
  });

  try {
    const first = ensureBuiltinDtcGrowthDatasource({
      metadataStore,
      userId: "user-a",
      workspaceId: "default",
      fixturePath: repoFixture,
      workspaceRoot
    });
    assert.equal(first.action, "created");
    assert.ok(first.filePath && existsSync(first.filePath));

    const record = metadataStore.dataSources.get({
      user_id: "user-a",
      datasource_id: DTC_GROWTH_DATASOURCE_ID
    });
    assert.equal(record.name, DTC_GROWTH_DATASOURCE_NAME);
    assert.equal(record.type, "sqlite");
    const config = JSON.parse(record.config_json);
    assert.equal(config.path, first.filePath);
    assert.equal(config.builtin, true);
    assert.equal(config.defaultEnabled, true);

    const second = ensureBuiltinDtcGrowthDatasource({
      metadataStore,
      userId: "user-a",
      workspaceId: "default",
      fixturePath: repoFixture,
      workspaceRoot
    });
    assert.equal(second.action, "skipped");
    assert.equal(
      metadataStore.dataSources.list({ user_id: "user-a" }).filter((item) => item.id === DTC_GROWTH_DATASOURCE_ID).length,
      1
    );

    // Broken path → repair without duplicating.
    rmSync(first.filePath, { force: true });
    metadataStore.dataSources.create({
      user_id: "user-a",
      id: DTC_GROWTH_DATASOURCE_ID,
      name: DTC_GROWTH_DATASOURCE_NAME,
      type: "sqlite",
      config: { ...config, path: join(root, "missing.sqlite") },
      status: "ready",
      expected_revision: record.revision
    });
    const repaired = ensureBuiltinDtcGrowthDatasource({
      metadataStore,
      userId: "user-a",
      workspaceId: "default",
      fixturePath: repoFixture,
      workspaceRoot
    });
    assert.equal(repaired.action, "repaired");
    assert.ok(repaired.filePath && existsSync(repaired.filePath));
    const repairedRecord = metadataStore.dataSources.get({
      user_id: "user-a",
      datasource_id: DTC_GROWTH_DATASOURCE_ID
    });
    assert.equal(JSON.parse(repairedRecord.config_json).path, repaired.filePath);

    // Deleted datasource is not revived.
    metadataStore.dataSources.delete({ user_id: "user-a", datasource_id: DTC_GROWTH_DATASOURCE_ID });
    const afterDelete = ensureBuiltinDtcGrowthDatasource({
      metadataStore,
      userId: "user-a",
      workspaceId: "default",
      fixturePath: repoFixture,
      workspaceRoot
    });
    assert.equal(afterDelete.action, "skipped");
    assert.equal(
      metadataStore.dataSources.find({ user_id: "user-a", datasource_id: DTC_GROWTH_DATASOURCE_ID })?.status,
      "deleted"
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("createServer auto-provisions DTC Growth Review and test-connect works", async () => {
  assert.ok(existsSync(repoFixture), `fixture missing: ${repoFixture}`);
  const root = mkdtempSync(join(tmpdir(), "dtc-growth-server-"));
  process.env.DATAFOUNDRY_AUTH_MODE = "password";
  process.env.AUTH_SESSION_SECRET = "dtc-growth-server-session-secret-32b!";
  process.env.AUTH_PUBLIC_BASE_URL = "http://127.0.0.1:3000";
  process.env.AUTH_EMAIL_DELIVERY = "test";
  process.env.AUTH_REGISTRATION_MODE = "open";
  process.env.STORAGE_ROOT_DIR = root;
  process.env.WORKSPACE_ROOT = join(root, "workspaces");
  process.env.MASTRA_STORAGE_PATH = join(root, "mastra.sqlite");
  process.env.EMBEDDING_API_KEY = "";
  process.env.DTC_GROWTH_FIXTURE_PATH = repoFixture;

  const metadataStore = createMetadataStore({
    database_path: join(root, "metadata.sqlite"),
    secret_master_key: "dtc-growth-server-test-key"
  });
  const dataGateway = new LocalDataGateway(metadataStore);
  const server = await createApiServer({ metadataStore });
  await new Promise((resolveListen) => server.listen(0, "127.0.0.1", resolveListen));
  const address = server.address();
  assert(address && typeof address === "object");
  const baseUrl = `http://127.0.0.1:${address.port}`;
  const client = createAuthenticatedTestClient({ baseUrl });
  const identity = await client.registerAndLogin({ displayName: "Builtin DTC Growth" });
  const { userId, workspaceId } = identity;

  try {
    const listed = await client.fetch("/api/v1/datasources");
    assert.equal(listed.status, 200);
    const listedBody = await listed.json();
    const items = listedBody.data ?? listedBody;
    const dtc = (Array.isArray(items) ? items : []).find((item) => item.id === DTC_GROWTH_DATASOURCE_ID);
    assert.ok(dtc, `expected ${DTC_GROWTH_DATASOURCE_ID} in ${JSON.stringify(listedBody)}`);
    assert.equal(dtc.name, DTC_GROWTH_DATASOURCE_NAME);
    assert.equal(dtc.type, "sqlite");
    assert.equal(dtc.builtin, true);
    assert.ok(typeof dtc.config?.path === "string" && existsSync(dtc.config.path));

    const testConnect = await client.fetch(`/api/v1/datasources/${DTC_GROWTH_DATASOURCE_ID}/test`, {
      method: "POST"
    });
    const testBody = await testConnect.json();
    assert.equal(testConnect.status, 200, JSON.stringify(testBody));
    assert.equal(testBody.data?.ok, true, JSON.stringify(testBody));

    // Gateway query against provisioned path.
    const schema = await dataGateway.inspectSchema({
      user_id: userId,
      workspace_id: workspaceId,
      datasource_id: DTC_GROWTH_DATASOURCE_ID
    });
    const tableNames = schema.tables.map((table) => table.name).sort();
    assert.ok(tableNames.includes("orders"));
    assert.ok(tableNames.includes("ad_spend"));

    // Second list remains single datasource (idempotent via memo + ensure).
    const listedAgain = await client.fetch("/api/v1/datasources");
    const againBody = await listedAgain.json();
    const againItems = againBody.data ?? againBody;
    const dtcCount = (Array.isArray(againItems) ? againItems : []).filter(
      (item) => item.id === DTC_GROWTH_DATASOURCE_ID
    ).length;
    assert.equal(dtcCount, 1);
  } finally {
    await new Promise((resolveClose, rejectClose) => {
      server.close((error) => (error ? rejectClose(error) : resolveClose()));
      setImmediate(() => server.closeAllConnections?.());
    });
    rmSync(root, { recursive: true, force: true });
  }
});
