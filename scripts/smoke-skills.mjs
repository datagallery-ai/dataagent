import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  buildSkillResourcePayload,
  materializeSkillPackages,
  parseSkillPackage,
  selectSkillsForRun
} from "../packages/skills/dist/index.js";
import { createSkillTools } from "@mastra/core/workspace";
import { LocalFileAssetService } from "../packages/files/dist/index.js";
import {
  createToolObservationBoundary,
  ToolObservationDispatcher
} from "../packages/agent-runtime/dist/testing.js";
import { createRunWorkspace } from "../packages/agent-runtime/dist/tools/workspace-factory.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const root = mkdtempSync(join(tmpdir(), "open-data-foundry-skills-smoke-"));
const metadataStore = createMetadataStore({ database_path: join(root, "metadata.sqlite") });
const __testIdentity = createVerifiedTestIdentity(metadataStore);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;

const fileAssetService = new LocalFileAssetService(metadataStore, { storageRoot: join(root, "files") });

const packageBody = Buffer.from(`---
name: sql-analysis-smoke
description: Use for SQL analysis smoke tests.
version: 1.0.0
tags:
  - data-analysis
  - sql
allowed-tools:
  - inspect_schema
  - run_sql_readonly
---
Inspect schema first, then run read-only SQL.
中文触发词: 数据分析 查数 指标查询.
`, "utf8");

const parsed = await parseSkillPackage({
  content: packageBody,
  filename: "SKILL.md",
  mimeType: "text/markdown"
});
assert.equal(parsed.name, "sql-analysis-smoke");
assert.deepEqual(parsed.allowedTools, ["inspect_schema", "run_sql_readonly"]);

const packageRef = fileAssetService.createRef({
  user_id: userId,
  workspace_id: workspaceId,
  filename: "SKILL.md",
  content: packageBody,
  declared_mime_type: "text/markdown",
  source: "skill-package",
  metadata: { kind: "skill-package" }
});
const resource = metadataStore.configResources.upsert({
  id: "sql-analysis-smoke",
  workspace_id: workspaceId,
  user_id: userId,
  kind: "skill",
  name: parsed.name,
  description: parsed.description,
  payload: buildSkillResourcePayload({
    packageFileRefId: packageRef.ref.id,
    parsed
  }),
  default_enabled: true,
  status: "valid"
});

const selection = selectSkillsForRun({
  metadataStore,
  runConfig: {
    enabledSkillIds: [],
    skillIds: [],
    skillMode: "auto",
    skillPolicy: {
      deniedToolNames: [],
      maxSkills: 5,
      requireUserInvocable: true,
      strictSkillTools: false
    },
    skillTags: []
  },
  userId,
  userInput: "analyze data with sql",
  workspaceId
});
assert.equal(selection.selectedSkills[0]?.id, resource.id);
assert.equal(selection.effectiveToolPolicy.mergeStrategy, "union");
assert.equal(selection.effectiveToolPolicy.allowedTools?.includes("run_sql_readonly"), true);

const materialized = await materializeSkillPackages({
  fileAssetService,
  runDir: join(root, "workspace"),
  skills: selection.selectedSkills,
  userId,
  workspaceId
});
assert.equal(materialized[0]?.path, "skills/sql-analysis-smoke");
const materializedSkillPath = join(root, "workspace", "skills", "sql-analysis-smoke", "SKILL.md");
assert.equal(existsSync(materializedSkillPath), true);
assert.equal(readFileSync(materializedSkillPath, "utf8").includes("Inspect schema first"), true);

const runContext = {
  user_id: userId,
  workspace_id: workspaceId,
  session_id: "skill-search-session",
  run_id: "skill-search-run",
  selected_datasource_id: "skill-smoke-source",
  enabled_datasource_ids: ["skill-smoke-source"],
  user_input: "分析数据",
  chat_mode: "copilotkit",
  model_name: "skill-smoke-model"
};
const searchWorkspaceRoot = join(root, "runtime-workspace");
const searchWorkspace = createRunWorkspace({ runContext, workspaceRoot: searchWorkspaceRoot });
const searchMaterialized = await materializeSkillPackages({
  fileAssetService,
  runDir: searchWorkspace.skillCacheDir,
  skills: selection.selectedSkills,
  userId,
  workspaceId
});
assert.equal(searchMaterialized[0]?.path, "skills/sql-analysis-smoke");
const unselectedSkillBody = Buffer.from(`---
name: unselected-report-smoke
description: Use for unselected report smoke tests.
version: 1.0.0
tags:
  - report
allowed-tools:
  - write_file
---
中文触发词: 报告草稿.
`, "utf8");
const unselectedSkillRef = fileAssetService.createRef({
  user_id: userId,
  workspace_id: workspaceId,
  filename: "SKILL.md",
  content: unselectedSkillBody,
  declared_mime_type: "text/markdown",
  source: "skill-package",
  metadata: { kind: "skill-package", skill: "unselected-report-smoke", version: "1.0.0" }
});
await materializeSkillPackages({
  fileAssetService,
  runDir: searchWorkspace.skillCacheDir,
  skills: [{
    allowedTools: ["write_file"],
    builtin: false,
    defaultDbIds: [],
    defaultEnabled: false,
    defaultKbIds: [],
    defaultMcpIds: [],
    deniedTools: [],
    description: "Use for unselected report smoke tests.",
    id: "unselected-report-smoke",
    name: "unselected-report-smoke",
    packageEntry: "SKILL.md",
    packageFileRefId: unselectedSkillRef.ref.id,
    packageFiles: ["SKILL.md"],
    packageFormat: "skill-md",
    revision: 1,
    scope: "workspace",
    status: "valid",
    tags: ["report"],
    userInvocable: true,
    version: "1.0.0"
  }],
  userId,
  workspaceId
});
assert.equal(searchWorkspace.skillCacheDir.startsWith(searchWorkspace.runDir), false);
assert.equal(
  existsSync(join(searchWorkspace.sessionDir, "skills", "sql-analysis-smoke", "SKILL.md")),
  false
);
assert.equal(
  existsSync(join(searchWorkspace.runDir, "skills", "sql-analysis-smoke", "SKILL.md")),
  false
);
const searchWorkspaceWithSkills = createRunWorkspace({
  runContext,
  skillPaths: ["skills"],
  workspaceRoot: searchWorkspaceRoot
});
await searchWorkspaceWithSkills.workspace.init();
const searchSkillTools = createSkillTools(searchWorkspaceWithSkills.workspace.skills);
const searchResult = await searchSkillTools.skill_search.execute({ query: "查数 指标", topK: 3 }, {
  context: { requestContext: new Map() },
  mastra: undefined,
  name: "skill_search",
  workspace: searchWorkspaceWithSkills.workspace
});
assert(String(searchResult).includes("sql-analysis-smoke"), "skill_search should find selected cached skills");
const unselectedSearchResult = await searchSkillTools.skill_search.execute({ query: "报告草稿", topK: 3 }, {
  context: { requestContext: new Map() },
  mastra: undefined,
  name: "skill_search",
  workspace: searchWorkspaceWithSkills.workspace
});
assert(
  String(unselectedSearchResult).includes("unselected-report-smoke"),
  "skill_search should search the full shared skill cache, not just selected skills"
);
await searchWorkspaceWithSkills.destroy();
await searchWorkspace.destroy();

const builtinPackageBody = Buffer.from(`---
name: builtin-data-analysis
description: Use for builtin data analysis smoke tests.
version: 1.0.0
tags:
  - data
  - analysis
  - sql
allowed-tools:
  - inspect_schema
  - run_sql_readonly
---
Explore schema, run read-only SQL, validate results, and present evidence-backed findings.
`, "utf8");
const builtinParsed = await parseSkillPackage({
  content: builtinPackageBody,
  filename: "SKILL.md",
  mimeType: "text/markdown"
});
const builtinPackageRef = fileAssetService.createRef({
  user_id: userId,
  workspace_id: workspaceId,
  filename: "SKILL.md",
  content: builtinPackageBody,
  declared_mime_type: "text/markdown",
  source: "skill-package",
  metadata: { builtin: true, kind: "skill-package" }
});
const builtinResource = metadataStore.configResources.upsert({
  id: "builtin-data-analysis",
  workspace_id: workspaceId,
  user_id: userId,
  kind: "skill",
  name: builtinParsed.name,
  description: builtinParsed.description,
  payload: {
    ...buildSkillResourcePayload({
      fields: { packageSource: "builtin://builtin-data-analysis" },
      packageFileRefId: builtinPackageRef.ref.id,
      parsed: builtinParsed
    }),
    builtinSource: "builtin://builtin-data-analysis"
  },
  builtin: true,
  default_enabled: false,
  status: "valid"
});
const builtinSelection = selectSkillsForRun({
  metadataStore,
  runConfig: {
    activeSkillId: "builtin-data-analysis",
    enabledSkillIds: ["builtin-data-analysis"],
    skillIds: [],
    skillMode: "selected",
    skillPolicy: {
      deniedToolNames: [],
      maxSkills: 5,
      requireUserInvocable: true,
      strictSkillTools: false
    },
    skillTags: []
  },
  userId,
  userInput: "用 SQL 分析数据",
  workspaceId
});
assert.equal(builtinSelection.selectedSkills[0]?.id, builtinResource.id);
const builtinMaterialized = await materializeSkillPackages({
  fileAssetService,
  runDir: join(root, "builtin-workspace"),
  skills: builtinSelection.selectedSkills,
  userId,
  workspaceId
});
assert.equal(builtinMaterialized[0]?.path, "skills/builtin-data-analysis");
const builtinSkillPath = join(root, "builtin-workspace", "skills", "builtin-data-analysis", "SKILL.md");
assert.equal(existsSync(builtinSkillPath), true);
assert.equal(readFileSync(builtinSkillPath, "utf8").includes("Explore schema, run read-only SQL"), true);

const boundary = createToolObservationBoundary({
  identity: {
    resourceId: userId,
    runId: "skill-smoke-run",
    sessionId: "skill-smoke-session"
  }
});
const dispatcher = new ToolObservationDispatcher(boundary.packager, {
  modelName: "skill-smoke-model",
  resourceId: userId,
  runId: "skill-smoke-run",
  sessionId: "skill-smoke-session"
});
const contextPackage = dispatcher.dispatch("skill", {
  name: "sql-analysis-smoke",
  content: "Inspect schema first, then run read-only SQL."
});
assert.equal(contextPackage.items.some((item) => item.sourceType === "skill-activation"), true);

metadataStore.close();
console.log(
  "Skills smoke OK: package parsing, FileAsset ref, user/builtin selection, materialization, context adapter"
);
