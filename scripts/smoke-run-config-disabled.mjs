import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// A non-mock model provider is required by resolveRunConfig (server-default falls back
// to env). We don't make a real call; we only need the resolver to construct a provider.
process.env.LLM_PROVIDER = "openai-compatible";
process.env.LLM_MODEL = "r020-smoke-model";
process.env.LLM_API_KEY = "r020-smoke-key";

import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { resolveRunConfig } from "../apps/api/dist/run-config-resolver.js";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const root = mkdtempSync(join(tmpdir(), "r020-"));
const store = createMetadataStore({ database_path: join(root, "m.sqlite") });
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;


try {
  // Seed one enabled KB and one default_enabled=false KB.
  store.configResources.upsert({
    id: "kb-enabled",
    workspace_id: workspaceId,
    user_id: userId,
    kind: "knowledge-base",
    name: "enabled KB",
    payload: {},
    default_enabled: true,
    status: "ready"
  });
  store.configResources.upsert({
    id: "kb-disabled",
    workspace_id: workspaceId,
    user_id: userId,
    kind: "knowledge-base",
    name: "disabled-by-policy KB",
    payload: {},
    default_enabled: false,
    status: "ready"
  });
  // Seed a ready datasource so validation passes.
  store.dataSources.create({
    id: "ds-1",
    user_id: userId,
    name: "ds",
    type: "sqlite",
    config: "{}",
    status: "ready",
    revision: 1
  });

  // Run config sends BOTH kbs in enabledKnowledgeIds (session default-all-enabled style).
  const resolved = resolveRunConfig({
    defaultDatasourceId: "ds-1",
    metadataStore: store,
    userId,
    userInput: "test",
    workspaceId,
    runInput: {
      threadId: "t1",
      runId: "r1",
      messages: [],
      context: [],
      state: {},
      forwardedProps: {
        run_config: {
          activeDatasourceId: "ds-1",
          enabledDatasourceIds: ["ds-1"],
          enabledKnowledgeIds: ["kb-enabled", "kb-disabled"]
        }
      }
    }
  });

  const cfg = resolved.effectiveRunConfig;
  // R-020: the default_enabled=false KB is silently dropped, not thrown.
  assert.deepEqual(cfg.enabledKnowledgeIds, ["kb-enabled"], "disabled-by-policy KB should be dropped from enabled set");
  assert.ok(cfg.disabledByPolicy, "disabledByPolicy diagnostic should be populated");
  assert.equal(cfg.disabledByPolicy.length, 1, "one KB should be reported as disabled_by_policy");
  assert.equal(cfg.disabledByPolicy[0].id, "kb-disabled");
  assert.equal(cfg.disabledByPolicy[0].kind, "knowledge-base");

  console.log("R-020 smoke OK: default_enabled=false KB dropped + reported as disabled_by_policy, run continues");
} finally {
  store.close();
  rmSync(root, { force: true, recursive: true });
}
