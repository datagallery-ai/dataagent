import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

process.env.LLM_PROVIDER = "openai-compatible";
process.env.LLM_MODEL = "mcp-smoke-model";
process.env.LLM_API_KEY = "mcp-smoke-key";

import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { resolveRunConfig } from "../apps/api/dist/run-config-resolver.js";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const root = mkdtempSync(join(tmpdir(), "mcp-degraded-"));
const store = createMetadataStore({ database_path: join(root, "m.sqlite") });
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;


try {
  store.configResources.upsert({
    id: "mcp-valid",
    workspace_id: workspaceId,
    user_id: userId,
    kind: "mcp-server",
    name: "valid MCP",
    payload: {
      transport: "streamable-http",
      serverUrl: "http://127.0.0.1:9999",
      toolManifest: [{ name: "ping" }]
    },
    default_enabled: true,
    status: "ready"
  });
  store.configResources.upsert({
    id: "mcp-broken",
    workspace_id: workspaceId,
    user_id: userId,
    kind: "mcp-server",
    name: "broken MCP",
    payload: {
      transport: "streamable-http",
      serverUrl: "http://127.0.0.1:9998"
    },
    default_enabled: true,
    status: "ready"
  });
  store.configResources.upsert({
    id: "skill-static-tools",
    workspace_id: workspaceId,
    user_id: userId,
    kind: "skill",
    name: "Static tool skill",
    payload: {
      allowedTools: ["inspect_schema"],
      description: "Skill that only declares built-in data tools.",
      name: "Static tool skill",
      packageFileRefId: "skill-package-ref",
      tags: ["test"],
      userInvocable: true,
      version: "1.0.0"
    },
    default_enabled: false,
    status: "valid"
  });
  store.dataSources.create({
    id: "ds-1",
    user_id: userId,
    name: "ds",
    type: "sqlite",
    config: "{}",
    status: "ready",
    revision: 1
  });

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
          enabledMcpServerIds: ["mcp-valid", "mcp-broken"],
          activeSkillId: "skill-static-tools",
          enabledSkillIds: ["skill-static-tools"]
        }
      }
    }
  });

  const cfg = resolved.effectiveRunConfig;
  assert.deepEqual(cfg.enabledMcpServerIds, ["mcp-valid"], "broken MCP should be dropped from enabled set");
  assert.equal(resolved.mcpRuntime.servers.length, 1);
  assert.equal(resolved.mcpRuntime.servers[0]?.serverId, "mcp-valid");
  assert.deepEqual(resolved.mcpRuntime.toolNames, ["ping"]);
  assert.deepEqual(resolved.skillSelection.effectiveToolPolicy.allowedTools, ["inspect_schema"]);
  assert.ok(cfg.unavailableResources, "unavailableResources diagnostic should be populated");
  assert.equal(cfg.unavailableResources.length, 1);
  assert.equal(cfg.unavailableResources[0].id, "mcp-broken");
  assert.match(cfg.unavailableResources[0].reason, /MCP_TOOL_MANIFEST_REQUIRED:mcp-broken/);

  store.configResources.upsert({
    id: "skill-bound-mcp",
    workspace_id: workspaceId,
    user_id: userId,
    kind: "skill",
    name: "Skill-bound MCP",
    payload: {
      allowedTools: ["inspect_schema"],
      defaultMcpIds: ["mcp-valid"],
      description: "Skill that binds an MCP server but does not allow its MCP tools.",
      name: "Skill-bound MCP",
      packageFileRefId: "skill-bound-package-ref",
      tags: ["test"],
      userInvocable: true,
      version: "1.0.0"
    },
    default_enabled: false,
    status: "valid"
  });
  assert.throws(() => resolveRunConfig({
    defaultDatasourceId: "ds-1",
    metadataStore: store,
    userId,
    userInput: "test",
    workspaceId,
    runInput: {
      threadId: "t2",
      runId: "r2",
      messages: [],
      context: [],
      state: {},
      forwardedProps: {
        run_config: {
          activeDatasourceId: "ds-1",
          enabledDatasourceIds: ["ds-1"],
          activeSkillId: "skill-bound-mcp",
          enabledSkillIds: ["skill-bound-mcp"]
        }
      }
    }
  }), /SKILL_MCP_TOOL_POLICY_UNSUPPORTED:ping/);

  console.log("MCP degraded smoke OK: invalid MCP dropped, run continues with valid server");
} finally {
  store.close();
  rmSync(root, { force: true, recursive: true });
}
