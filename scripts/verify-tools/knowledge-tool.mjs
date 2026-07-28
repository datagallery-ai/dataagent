#!/usr/bin/env node
/**
 * Verification for retrieve_knowledge: registration, guard, e2e retrieve, data-* chunks.
 */
import { mkdirSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";

import {
  createDataFoundry,
  createDataFoundryRunContext,
} from "../../packages/agent-runtime/dist/index.js";
import { LocalDataGateway } from "../../packages/data-gateway/dist/index.js";
import { LocalKnowledgeService } from "../../packages/knowledge/dist/index.js";
import { createMetadataStore } from "../../packages/metadata/dist/index.js";
import { createVerifiedTestIdentity } from "../lib/metadata-test-identity.mjs";

const envPath = join(process.cwd(), ".env");
try {
  const envText = readFileSync(envPath, "utf8");
  for (const line of envText.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    const value = trimmed.slice(eq + 1).trim();
    if (!process.env[key]) process.env[key] = value;
  }
} catch {
  // optional .env
}

const stamp = Date.now();
const storageDir = `storage/verify-tools/knowledge-${stamp}`;
mkdirSync(storageDir, { recursive: true });
const metadataPath = `${storageDir}/metadata.sqlite`;

const store = createMetadataStore({
  database_path: metadataPath,
  ...(process.env.SECRET_MASTER_KEY ? { secret_master_key: process.env.SECRET_MASTER_KEY } : {}),
});
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;


const user_id = userId;
const session_id = `verify-kb-session-${stamp}`;
const run_id = `verify-kb-run-${stamp}`;
const datasource_id = "api-duckdb-demo";
const collection_id = "verify-kb";

store.dataSources.create({
  user_id,
  id: datasource_id,
  name: "API DuckDB Demo",
  type: "duckdb",
  config: { mode: "demo" },
});
store.sessions.create({
  user_id,
  id: session_id,
  title: "verify knowledge tool",
  selected_datasource_id: datasource_id,
});
store.runs.create({
  user_id,
  id: run_id,
  session_id,
  user_input: "verify retrieve_knowledge",
  status: "running",
  datasource_id,
});

const embedding = {
  provider: process.env.EMBEDDING_PROVIDER ?? "",
  model: process.env.EMBEDDING_MODEL ?? "",
  base_url: process.env.EMBEDDING_BASE_URL ?? "",
  api_key: process.env.EMBEDDING_API_KEY ?? "",
};
const hasEmbeddingCreds = Boolean(embedding.api_key && embedding.base_url && embedding.model);

const knowledgeService = new LocalKnowledgeService(store, {
  embedding: {
    provider: embedding.provider,
    model: embedding.model,
    base_url: embedding.base_url,
    ...(embedding.api_key ? { api_key: embedding.api_key } : {}),
  },
});

const gateway = new LocalDataGateway(store);

const fakeModelProvider = {
  kind: "openai-compatible",
  model: "openai/verify",
  model_name: "verify",
};

const runContextInput = {
  user_id,
  session_id,
  run_id,
  user_input: "verify retrieve_knowledge",
  chat_mode: "copilotkit",
  selected_datasource_id: datasource_id,
  enabled_datasource_ids: [datasource_id],
  model_name: "verify",
};

function makeExecCtx(toolName) {
  const customChunks = [];
  return {
    execCtx: {
      context: { requestContext: new Map() },
      mastra: undefined,
      agentName: "verify",
      name: toolName,
      writer: {
        custom: async (c) => customChunks.push(c),
        write: async (c) => customChunks.push({ write: c }),
      },
    },
    customChunks,
  };
}

function dataChunkTypes(chunks) {
  return [
    ...new Set(
      chunks
        .map((c) => (c && typeof c === "object" && "type" in c ? c.type : null))
        .filter((t) => typeof t === "string" && t.startsWith("data-")),
    ),
  ];
}

function chunkCountFromResult(result) {
  if (!result || typeof result !== "object") return 0;
  if (Array.isArray(result.chunks)) return result.chunks.length;
  if (Array.isArray(result)) return result.length;
  return 0;
}

const report = {
  registered: false,
  registeredWithoutService: true,
  guardWorks: false,
  guardError: null,
  e2e: "blocked-by-credentials",
  e2eChunkCount: 0,
  dataChunkTypes: [],
  notes: [],
};

let exitCode = 0;
let configuredWithKb;
let configuredNoKb;
let configuredE2e;

try {
  configuredWithKb = await createDataFoundry({
    dataGateway: gateway,
    emitter: { emit: () => undefined },
    knowledgeService,
    messages: [],
    modelProvider: fakeModelProvider,
    runContext: createDataFoundryRunContext({
      ...runContextInput,
      enabled_knowledge_ids: [],
    }),
    workspaceRoot: `${storageDir}/workspace-with-kb`,
  });

  const toolNamesWith = Object.keys(await configuredWithKb.agent.listTools());
  report.registered = toolNamesWith.includes("retrieve_knowledge");
  if (!report.registered) {
    exitCode = 1;
    report.notes.push("retrieve_knowledge missing when knowledgeService is provided");
  }

  configuredNoKb = await createDataFoundry({
    dataGateway: gateway,
    emitter: { emit: () => undefined },
    messages: [],
    modelProvider: fakeModelProvider,
    runContext: createDataFoundryRunContext({
      ...runContextInput,
      enabled_knowledge_ids: [],
    }),
    workspaceRoot: `${storageDir}/workspace-no-kb`,
  });
  const toolNamesNo = Object.keys(await configuredNoKb.agent.listTools());
  report.registeredWithoutService = !toolNamesNo.includes("retrieve_knowledge");
  if (!report.registeredWithoutService) {
    exitCode = 1;
    report.notes.push("retrieve_knowledge present without knowledgeService");
  }

  const toolsWith = await configuredWithKb.agent.listTools();
  const retrieveTool = toolsWith.retrieve_knowledge;
  if (!retrieveTool?.execute) {
    exitCode = 1;
    report.notes.push("retrieve_knowledge tool has no execute");
  } else {
    const { execCtx, customChunks } = makeExecCtx("retrieve_knowledge");
    try {
      await retrieveTool.execute(
        { collection_id: "not-enabled", query: "x" },
        execCtx,
      );
      report.guardError = "expected KNOWLEDGE_BASE_NOT_ENABLED throw";
      exitCode = 1;
    } catch (err) {
      report.guardError = err instanceof Error ? err.message : String(err);
      report.guardWorks =
        report.guardError === "KNOWLEDGE_BASE_NOT_ENABLED:not-enabled";
      if (!report.guardWorks) exitCode = 1;
    }
    report.dataChunkTypes = dataChunkTypes(customChunks);
    if (report.dataChunkTypes.length === 0) {
      report.notes.push("no data-* chunks via writer.custom on guard call");
    }
  }

  if (hasEmbeddingCreds) {
    store.configResources.upsert({
      id: collection_id,
      workspace_id: workspaceId,
      user_id,
      kind: "knowledge-base",
      name: "Verify KB",
      payload: {
        embeddingProvider: embedding.provider,
        embeddingModel: embedding.model,
        embeddingBaseUrl: embedding.base_url,
      },
    });

    await knowledgeService.ingestText({
      user_id,
      collection_id,
      filename: "metrics.md",
      content: "Revenue metric is gross sales before refunds.",
    });
    await knowledgeService.ingestText({
      user_id,
      collection_id,
      filename: "churn.md",
      content: "Customer churn rate measures subscription cancellations over time.",
    });

    configuredE2e = await createDataFoundry({
      dataGateway: gateway,
      emitter: { emit: () => undefined },
      knowledgeService,
      messages: [],
      modelProvider: fakeModelProvider,
      runContext: createDataFoundryRunContext({
        ...runContextInput,
        enabled_knowledge_ids: [collection_id],
      }),
      workspaceRoot: `${storageDir}/workspace-e2e`,
    });

    const e2eTools = await configuredE2e.agent.listTools();
    const e2eTool = e2eTools.retrieve_knowledge;
    const { execCtx: e2eCtx, customChunks: e2eChunks } = makeExecCtx("retrieve_knowledge");

    try {
      const result = await e2eTool.execute(
        { collection_id, query: "revenue metric", top_k: 3 },
        e2eCtx,
      );
      report.e2eChunkCount = chunkCountFromResult(result);
      report.e2e =
        report.e2eChunkCount > 0 ? "ok" : "failed-empty-chunks";
      if (report.e2eChunkCount === 0) exitCode = 1;

      const e2eDataTypes = dataChunkTypes(e2eChunks);
      if (e2eDataTypes.length > 0) {
        report.dataChunkTypes = [...new Set([...report.dataChunkTypes, ...e2eDataTypes])];
      }
      report.notes.push(
        `e2e governed result keys: ${result && typeof result === "object" ? Object.keys(result).join(",") : typeof result}`,
      );
    } catch (err) {
      report.e2e = "failed";
      report.notes.push(
        `e2e error: ${err instanceof Error ? err.message : String(err)}`,
      );
      exitCode = 1;
    }
  } else {
    report.notes.push(
      "e2e skipped: missing EMBEDDING_API_KEY and/or EMBEDDING_BASE_URL in .env",
    );
  }

  console.log(
    JSON.stringify(
      {
        exitCode,
        storageDir,
        hasEmbeddingCreds,
        report,
      },
      null,
      2,
    ),
  );
  process.exitCode = exitCode;
} finally {
  store.close();
  if (configuredWithKb) await configuredWithKb.destroyWorkspace();
  if (configuredNoKb) await configuredNoKb.destroyWorkspace();
  if (configuredE2e) await configuredE2e.destroyWorkspace();
  rmSync(storageDir, { force: true, recursive: true });
}
